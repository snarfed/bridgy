"""Task queue handlers.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import bz2
import calendar
import copy
import datetime
import gc
import json
import logging
import random
import urlparse

from google.appengine.api import memcache
from google.appengine.api import datastore_errors
from google.appengine.api.datastore_types import _MAX_STRING_LENGTH
from google.appengine.ext import ndb
from granary import source as gr_source
import webapp2
from webmentiontools import send

import appengine_config

from oauth_dropins import handlers
from granary.source import Source
# need to import model class definitions since poll creates and saves entities.
import blogger
import facebook
import flickr
import googleplus
import instagram
import models
from models import Response
import original_post_discovery
import tumblr
import twitter
import util
import wordpress_rest

WEBMENTION_DISCOVERY_CACHE_TIME = 60 * 60 * 2  # 2h

ERROR_HTTP_RETURN_CODE = 304  # "Not Modified"


class Poll(webapp2.RequestHandler):
  """Task handler that fetches and processes new responses from a single source.

  Request parameters:
    source_key: string key of source entity
    last_polled: timestamp, YYYY-MM-DD-HH-MM-SS

  Inserts a propagate task for each response that hasn't been seen before.
  """

  def post(self, *path_args):
    logging.debug('Params: %s', self.request.params)

    key = self.request.params['source_key']
    source = ndb.Key(urlsafe=key).get()
    if not source or source.status == 'disabled' or 'listen' not in source.features:
      logging.error('Source not found or disabled. Dropping task.')
      return
    logging.info('Source: %s %s, %s', source.label(), source.key.string_id(),
                 source.bridgy_url(self))

    last_polled = self.request.params['last_polled']
    if last_polled != source.last_polled.strftime(util.POLL_TASK_DATETIME_FORMAT):
      logging.warning('duplicate poll task! deferring to the other task.')
      return

    logging.info('Last poll: %s/log?start_time=%s&key=%s',
                 self.request.host_url,
                 calendar.timegm(source.last_poll_attempt.utctimetuple()),
                 source.key.urlsafe())

    # mark this source as polling
    source.updates = {
      'poll_status': 'polling',
      'last_poll_attempt': util.now_fn(),
    }
    source = models.Source.put_updates(source)

    source.updates = {}
    try:
      self.poll(source)
    except models.DisableSource:
      # the user deauthorized the bridgy app, so disable this source.
      # let the task complete successfully so that it's not retried.
      source.updates['status'] = 'disabled'
      logging.warning('Disabling source!')
    except:
      source.updates['poll_status'] = 'error'
      raise
    finally:
      source = models.Source.put_updates(source)

    # add new poll task. randomize task ETA to within +/- 20% to try to spread
    # out tasks and prevent thundering herds.
    task_countdown = source.poll_period().total_seconds() * random.uniform(.8, 1.2)
    util.add_poll_task(source, countdown=task_countdown)

    # feeble attempt to avoid hitting the instance memory limit
    source = None
    gc.collect()

  def poll(self, source):
    """Actually runs the poll.

    Stores property names and values to update in source.updates.
    """
    if source.last_activities_etag or source.last_activity_id:
      logging.debug('Using ETag %s, last activity id %s',
                    source.last_activities_etag, source.last_activity_id)

    #
    # Step 1: fetch activities:
    # * posts by the user
    # * search all posts for the user's domain URLs to find links
    #
    cache = util.CacheDict()
    if source.last_activities_cache_json:
      cache.update(json.loads(source.last_activities_cache_json))

    try:
      # search for links first so that the user's activities and responses
      # override them if they overlap
      links = source.search_for_links()

      # this user's own activities (and user mentions)
      resp = source.get_activities_response(
        fetch_replies=True, fetch_likes=True, fetch_shares=True,
        fetch_mentions=True, count=50, etag=source.last_activities_etag,
        min_id=source.last_activity_id, cache=cache)
      etag = resp.get('etag')  # used later
      user_activities = resp.get('items', [])

      # these map ids to AS objects
      responses = {a['id']: a for a in links}
      activities = {a['id']: a for a in links + user_activities}

    except Exception, e:
      code, body = util.interpret_http_exception(e)
      if code == '401':
        msg = 'Unauthorized error: %s' % e
        logging.warning(msg, exc_info=True)
        source.updates['poll_status'] = 'ok'
        raise models.DisableSource(msg)
      elif code in util.HTTP_RATE_LIMIT_CODES:
        logging.warning('Rate limited. Marking as error and finishing. %s', e)
        source.updates.update({'poll_status': 'error', 'rate_limited': True})
        return
      elif (code and int(code) / 100 == 5) or util.is_connection_failure(e):
        logging.error('API call failed. Marking as error and finishing. %s: %s\n%s',
                      code, body, e)
        self.abort(ERROR_HTTP_RETURN_CODE)
      else:
        raise

    logging.info('Found %d activities: %s', len(activities), activities.keys())

    # extract silo activity ids, update last_activity_id
    silo_activity_ids = set()
    last_activity_id = source.last_activity_id
    for id, activity in activities.items():
      # maybe replace stored last activity id
      parsed = util.parse_tag_uri(id)
      if parsed:
        id = parsed[1]
      silo_activity_ids.add(id)
      try:
        # try numeric comparison first
        greater = int(id) > int(last_activity_id)
      except (TypeError, ValueError):
        greater = id > last_activity_id
      if greater:
        last_activity_id = id

    if last_activity_id and last_activity_id != source.last_activity_id:
      source.updates['last_activity_id'] = last_activity_id
      logging.debug('Storing new last activity id: %s', last_activity_id)

    # trim cache to just the returned activity ids, so that it doesn't grow
    # without bound. (WARNING: depends on get_activities_response()'s cache key
    # format, e.g. 'PREFIX ACTIVITY_ID'!)
    source.updates['last_activities_cache_json'] = json.dumps(
      {k: v for k, v in cache.items() if k.split()[-1] in silo_activity_ids})

    # Make sure we only fetch the author's h-feed(s) the first time
    # discover is called
    is_first_discover = True

    #
    # Step 2: extract responses, store their activities in response['activities']
    #
    # WARNING: this creates circular references in link posts found by search
    # queries in step 1, since they are their own activity. We use
    # prune_activity() and prune_response() in step 4 to remove these before
    # serializing to JSON.
    #
    for id, activity in activities.items():
      if not Source.is_public(activity):
        logging.info('Skipping non-public activity %s', id)
        continue

      obj = activity.get('object') or activity

      # handle user mentions
      user_id = source.user_tag_id()
      if obj.get('author', {}).get('id') != user_id:
        for tag in obj.get('tags', []):
          urls = tag.get('urls')
          if tag.get('objectType') == 'person' and tag.get('id') == user_id and urls:
            activity['originals'], activity['mentions'] = \
              original_post_discovery.discover(
                source, activity, fetch_hfeed=is_first_discover,
                include_redirect_sources=False)
            is_first_discover = False
            activity['mentions'].update(u.get('value') for u in urls)
            responses[id] = activity
            break

      # handle quote mentions
      for att in obj.get('attachments', []):
        if (att.get('objectType') in ('note', 'article')
                and att.get('author', {}).get('id') == source.user_tag_id()):
          # now that we've confirmed that one exists, OPD will dig
          # into the actual attachments
          if 'originals' not in activity or 'mentions' not in activity:
            activity['originals'], activity['mentions'] = \
              original_post_discovery.discover(
                source, activity, fetch_hfeed=is_first_discover,
                include_redirect_sources=False)
            is_first_discover = False
          responses[id] = activity
          break

      # extract replies, likes, reposts, and rsvps
      replies = obj.get('replies', {}).get('items', [])
      tags = obj.get('tags', [])
      likes = [t for t in tags if Response.get_type(t) == 'like']
      reposts = [t for t in tags if Response.get_type(t) == 'repost']
      rsvps = Source.get_rsvps_from_event(obj)

      # coalesce responses. drop any without ids
      for resp in replies + likes + reposts + rsvps:
        id = resp.get('id')
        if not id:
          logging.error('Skipping response without id: %s', json.dumps(resp, indent=2))
          continue

        resp.setdefault('activities', []).append(activity)

        # when we find two responses with the same id, the earlier one may have
        # come from a link post or user mention, and this one is probably better
        # since it probably came from the user's activity, so prefer this one.
        # background: https://github.com/snarfed/bridgy/issues/533
        existing = responses.get(id)
        if existing:
          if source.gr_source.activity_changed(resp, existing, log=True):
            logging.warning('Got two different versions of same response!\n%s\n%s',
                            existing, resp)
          resp['activities'].extend(existing.get('activities', []))

        responses[id] = resp

    #
    # Step 3: filter out responses we've already seen
    #
    # seen responses (JSON objects) for each source are stored in its entity.
    unchanged_responses = []
    if source.seen_responses_cache_json:
      for seen in json.loads(source.seen_responses_cache_json):
        id = seen['id']
        resp = responses.get(id)
        if resp and not source.gr_source.activity_changed(seen, resp, log=True):
          unchanged_responses.append(seen)
          del responses[id]

    #
    # Step 4: store new responses and enqueue propagate tasks
    #
    pruned_responses = []
    for id, resp in responses.items():
      resp_type = Response.get_type(resp)
      activities = resp.pop('activities', [])
      if not activities and resp_type == 'post':
        activities = [resp]
      too_long = set()
      urls_to_activity = {}
      for i, activity in enumerate(activities):
        # we'll usually have multiple responses for the same activity, and the
        # objects in resp['activities'] are shared, so cache each activity's
        # discovered webmention targets inside its object.
        if 'originals' not in activity or 'mentions' not in activity:
          activity['originals'], activity['mentions'] = \
            original_post_discovery.discover(
              source, activity, fetch_hfeed=is_first_discover,
              include_redirect_sources=False)
          is_first_discover = False

        targets = original_post_discovery.targets_for_response(
          resp, originals=activity['originals'], mentions=activity['mentions'])
        if targets:
          logging.info('%s has %d webmention target(s): %s', activity.get('url'),
                       len(targets), ' '.join(targets))
        for t in targets:
          if len(t) <= _MAX_STRING_LENGTH:
            urls_to_activity[t] = i
          else:
            logging.warning('Giving up on target URL over %s chars! %s',
                            _MAX_STRING_LENGTH, t)
            too_long.add(t[:_MAX_STRING_LENGTH - 4] + '...')

      # store/update response entity. the prune_*() calls are important to
      # remove circular references in link responses, which are their own
      # activities. details in the step 2 comment above.
      pruned_response = util.prune_response(resp)
      pruned_responses.append(pruned_response)
      resp_entity = Response(
        id=id,
        source=source.key,
        activities_json=[json.dumps(util.prune_activity(a)) for a in activities],
        response_json=json.dumps(pruned_response),
        type=resp_type,
        unsent=list(urls_to_activity.keys()),
        failed=list(too_long),
        original_posts=resp.get('originals', []))
      if urls_to_activity and len(activities) > 1:
        resp_entity.urls_to_activity=json.dumps(urls_to_activity)
      resp_entity.get_or_save(source)

    # update cache
    if pruned_responses:
      source.updates['seen_responses_cache_json'] = json.dumps(
        pruned_responses + unchanged_responses)

    source.updates.update({'last_polled': source.last_poll_attempt,
                           'poll_status': 'ok'})
    if etag and etag != source.last_activities_etag:
      logging.debug('Storing new ETag: %s', etag)
      source.updates['last_activities_etag'] = etag

    #
    # Step 5. possibly refetch updated syndication urls
    #
    # if the author has added syndication urls since the first time
    # original_post_discovery ran, we'll miss them. this cleanup task will
    # periodically check for updated urls. only kicks in if the author has
    # *ever* published a rel=syndication url
    if (source.last_hfeed_refetch == models.REFETCH_HFEED_TRIGGER or
        (source.last_syndication_url and
         source.last_hfeed_refetch + source.refetch_period()
            <= source.last_poll_attempt)):
      logging.info('refetching h-feed for source %s', source.label())
      relationships = original_post_discovery.refetch(source)

      now = util.now_fn()
      logging.debug('updating source last_hfeed_refetch %s', now)
      source.updates['last_hfeed_refetch'] = now

      if relationships:
        logging.info('refetch h-feed found new rel=syndication relationships: %s',
                     relationships)
        try:
          self.repropagate_old_responses(source, relationships)
        except BaseException, e:
          if (isinstance(e, (datastore_errors.BadRequestError,
                             datastore_errors.Timeout)) or
              util.is_connection_failure(e)):
            logging.info('Timeout while repropagating responses.', exc_info=True)
          else:
            raise
    else:
      logging.info(
          'skipping refetch h-feed. last-syndication-url %s, last-refetch %s',
          source.last_syndication_url, source.last_hfeed_refetch)

  def repropagate_old_responses(self, source, relationships):
    """Find old Responses that match a new SyndicatedPost and repropagate them.

    We look through as many responses as we can until the datastore query expires.
    """
    for response in (Response.query(Response.source == source.key)
                     .order(-Response.updated)):
      if response.activity_json:  # handle old entities
        response.activities_json.append(response.activity_json)
        response.activity_json = None

      new_orig_urls = set()
      for activity_json in response.activities_json:
        activity = json.loads(activity_json)
        activity_url = activity.get('url') or activity.get('object', {}).get('url')
        if not activity_url:
          logging.warning('activity has no url %s', activity_json)
          continue

        activity_url = source.canonicalize_syndication_url(activity_url,
                                                           activity=activity)
        # look for activity url in the newly discovered list of relationships
        for relationship in relationships.get(activity_url, []):
          # won't re-propagate if the discovered link is already among
          # these well-known upstream duplicates
          if (relationship.original in response.sent or
              relationship.original in response.original_posts):
            logging.info(
              '%s found a new rel=syndication link %s -> %s, but the '
              'relationship had already been discovered by another method',
              response.label(), relationship.original,
              relationship.syndication)
          else:
            logging.info(
              '%s found a new rel=syndication link %s -> %s, and '
              'will be repropagated with a new target!',
              response.label(), relationship.original,
              relationship.syndication)
            new_orig_urls.add(relationship.original)

      if new_orig_urls:
        # re-open a previously 'complete' propagate task
        response.status = 'new'
        response.unsent.extend(list(new_orig_urls))
        response.put()
        response.add_task()


class SendWebmentions(webapp2.RequestHandler):
  """Abstract base task handler that can send webmentions.

  Attributes:
    entity: Webmentions subclass instance (set in lease_entity)
    source: Source entity (set in send_webmentions)
  """

  # request deadline (10m) plus some padding
  LEASE_LENGTH = datetime.timedelta(minutes=12)

  def source_url(self, target_url):
    """Return the source URL to use for a given target URL.

    Subclasses must implement.

    Args:
      target_url: string

    Returns: string
    """
    raise NotImplementedError()

  def send_webmentions(self):
    """Tries to send each unsent webmention in self.entity.

    Uses source_url() to determine the source parameter for each webmention.

    self.lease() *must* be called before this!
    """
    logging.info('Starting %s', self.entity.label())

    self.source = self.entity.source.get()
    try:
      self.do_send_webmentions()
    except:
      logging.warning('Propagate task failed', exc_info=True)
      self.release('error')
      raise

  def do_send_webmentions(self):
    urls = self.entity.unsent + self.entity.error + self.entity.failed
    unsent = set()
    self.entity.error = []
    self.entity.failed = []

    for orig_url in urls:
      # recheck the url here since the checks may have failed during the poll
      # or streaming add.
      url, domain, ok = util.get_webmention_target(orig_url)
      if ok:
        if len(url) <= _MAX_STRING_LENGTH:
          unsent.add(url)
        else:
          logging.warning('Giving up on target URL over %s chars! %s',
                          _MAX_STRING_LENGTH, url)
          self.entity.failed.append(orig_url)
    self.entity.unsent = sorted(unsent)

    while self.entity.unsent:
      target = self.entity.unsent.pop(0)
      source_url = self.source_url(target)
      logging.info('Webmention from %s to %s', source_url, target)

      # see if we've cached webmention discovery for this domain. the cache
      # value is a string URL endpoint if discovery succeeded, a
      # WebmentionSend error dict if it failed (semi-)permanently, or None.
      cache_key = util.webmention_endpoint_cache_key(target)
      cached = memcache.get(cache_key)
      if cached:
        logging.info('Using cached webmention endpoint %r: %s', cache_key, cached)

      # send! and handle response or error
      error = None
      if isinstance(cached, dict):
        error = cached
      else:
        mention = send.WebmentionSend(source_url, target, endpoint=cached)
        logging.info('Sending...')
        try:
          if not mention.send(timeout=999, headers=util.USER_AGENT_HEADER):
            error = mention.error
        except BaseException, e:
          logging.warning('', exc_info=True)
          error = getattr(mention, 'error')
          if not error:
            error = ({'code': 'BAD_TARGET_URL', 'http_status': 499}
                     if 'DNS lookup failed for URL:' in str(e)
                     else {'code': 'EXCEPTION'})

      if not cached:
        val = (error if error and error['code'] in ('NO_ENDPOINT', 'BAD_TARGET_URL')
               else mention.receiver_endpoint)
        memcache.set(cache_key, val, time=WEBMENTION_DISCOVERY_CACHE_TIME)

      if error is None:
        logging.info('Sent! %s', mention.response)
        self.record_source_webmention(mention)
        self.entity.sent.append(target)
      else:
        code = error['code']
        status = error.get('http_status', 0)
        if (code == 'NO_ENDPOINT' or
            (code == 'BAD_TARGET_URL' and status == 204)):  # 204 is No Content
          logging.info('Giving up this target. %s', error)
          self.entity.skipped.append(target)
        elif status // 100 == 4:
          # Give up on 4XX errors; we don't expect later retries to succeed.
          logging.info('Giving up this target. %s', error)
          self.entity.failed.append(target)
        else:
          self.fail('Error sending to endpoint: %s' % error)
          self.entity.error.append(target)

      if target in self.entity.unsent:
        self.entity.unsent.remove(target)

    if self.entity.error:
      logging.warning('Propagate task failed')
      self.release('error')
    else:
      self.complete()

  @ndb.transactional
  def lease(self, key):
    """Attempts to acquire and lease the Webmentions entity.

    Returns True on success, False or None otherwise.

    TODO: unify with complete()

    Args:
      key: ndb.Key
    """
    self.entity = key.get()

    if self.entity is None:
      self.fail('no entity!')
    elif self.entity.status == 'complete':
      # let this task return 200 and finish
      logging.warning('duplicate task already propagated this')
    elif (self.entity.status == 'processing' and
          util.now_fn() < self.entity.leased_until):
      self.fail('duplicate task is currently processing!')
    else:
      assert self.entity.status in ('new', 'processing', 'error'), self.entity.status
      self.entity.status = 'processing'
      self.entity.leased_until = util.now_fn() + self.LEASE_LENGTH
      self.entity.put()
      return True

  @ndb.transactional
  def complete(self):
    """Attempts to mark the Webmentions entity completed.

    Returns True on success, False otherwise.
    """
    existing = self.entity.key.get()
    if existing is None:
      self.fail('entity disappeared!', level=logging.ERROR)
    elif existing.status == 'complete':
      # let this task return 200 and finish
      logging.warning('another task stole and finished this. did my lease expire?')
      return False
    elif existing.status == 'new':
      self.fail('went backward from processing to new!', level=logging.ERROR)

    assert self.entity.status == 'processing', self.entity.status
    self.entity.status = 'complete'
    self.entity.put()
    return True

  @ndb.transactional
  def release(self, new_status):
    """Attempts to unlease the Webmentions entity.

    Args:
      new_status: string
    """
    existing = self.entity.key.get()
    if existing and existing.status == 'processing':
      self.entity.status = new_status
      self.entity.leased_until = None
      self.entity.put()

  def fail(self, message, level=logging.WARNING):
    """Fills in an error response status code and message.
    """
    self.error(ERROR_HTTP_RETURN_CODE)
    logging.log(level, message)
    self.response.out.write(message)

  @ndb.transactional
  def record_source_webmention(self, mention):
    """Sets this source's last_webmention_sent and maybe webmention_endpoint.

    Args:
      mention: webmentiontools.send.WebmentionSend
    """
    self.source = self.source.key.get()
    logging.info('Setting last_webmention_sent')
    self.source.last_webmention_sent = util.now_fn()

    if (mention.receiver_endpoint != self.source.webmention_endpoint and
        util.domain_from_link(mention.target_url) in self.source.domains):
      logging.info('Also setting webmention_endpoint to %s (discovered in %s; was %s)',
                   mention.receiver_endpoint, mention.target_url,
                   self.source.webmention_endpoint)
      self.source.webmention_endpoint = mention.receiver_endpoint

    self.source.put()


class PropagateResponse(SendWebmentions):
  """Task handler that sends webmentions for a Response.

  Attributes:
    activities: parsed Response.activities_json list

  Request parameters:
    response_key: string key of Response entity
  """

  def post(self):
    logging.debug('Params: %s', self.request.params)
    if not self.lease(ndb.Key(urlsafe=self.request.params['response_key'])):
      return

    self.activities = [json.loads(a) for a in self.entity.activities_json]
    response_obj = json.loads(self.entity.response_json)
    if (not Source.is_public(response_obj) or
        not all(Source.is_public(a) for a in self.activities)):
      logging.info('Response or activity is non-public. Dropping.')
      self.complete()
      return

    source = self.entity.source.get()
    if not source:
      logging.warning('Source not found! Dropping response.')
      return
    logging.info('Source: %s %s, %s', source.label(), source.key.string_id(),
                 source.bridgy_url(self))
    logging.info('Created by this poll: %s/log?start_time=%s&key=%s',
                 self.request.host_url,
                 calendar.timegm(self.entity.created.utctimetuple()) - 61,
                 source.key.urlsafe())

    self.send_webmentions()

  def source_url(self, target_url):
    # parse the response id. (we know Response key ids are always tag URIs)
    _, response_id = util.parse_tag_uri(self.entity.key.string_id())
    if self.entity.type in ('like', 'repost', 'rsvp'):
      response_id = response_id.split('_')[-1]

    # determine which activity to use
    activity = self.activities[0]
    if self.entity.urls_to_activity:
      urls_to_activity = json.loads(self.entity.urls_to_activity)
      if urls_to_activity:
        try:
          activity = self.activities[urls_to_activity[target_url]]
        except KeyError:
          logging.warning("""\
Hit https://github.com/snarfed/bridgy/issues/237 KeyError!
target url %s not in urls_to_activity: %s
activities: %s""", target_url, urls_to_activity, self.activities)
          self.abort(ERROR_HTTP_RETURN_CODE)

    # generate source URL
    id = activity['id']
    parsed = util.parse_tag_uri(id)
    post_id = parsed[1] if parsed else id
    # prefer brid-gy.appspot.com to brid.gy because non-browsers (ie OpenSSL)
    # currently have problems with brid.gy's SSL cert. details:
    # https://github.com/snarfed/bridgy/issues/20
    if (self.request.host_url.endswith('brid.gy') or
        self.request.host_url.endswith('brid-gy.appspot.com')):
      host_url = 'https://brid-gy.appspot.com'
    else:
      host_url = self.request.host_url

    path = [host_url, self.entity.type, self.entity.source.get().SHORT_NAME,
            self.entity.source.string_id(), post_id]
    if self.entity.type != 'post':
      path.append(response_id)
    return '/'.join(path)


class PropagateBlogPost(SendWebmentions):
  """Task handler that sends webmentions for a BlogPost.

  Request parameters:
    key: string key of BlogPost entity
  """

  def post(self):
    logging.debug('Params: %s', self.request.params)

    if self.lease(ndb.Key(urlsafe=self.request.params['key'])):
      source_domains = self.entity.source.get().domains
      to_send = set()
      for url in self.entity.unsent:
        url, domain, ok = util.get_webmention_target(url)
        # skip "self" links to this blog's domain
        if ok and domain not in source_domains:
          to_send.add(url)

      self.entity.unsent = list(to_send)
      self.send_webmentions()

  def source_url(self, target_url):
    return self.entity.key.id()


application = webapp2.WSGIApplication([
    ('/_ah/queue/poll(-now)?', Poll),
    ('/_ah/queue/propagate', PropagateResponse),
    ('/_ah/queue/propagate-blogpost', PropagateBlogPost),
    ], debug=appengine_config.DEBUG)
