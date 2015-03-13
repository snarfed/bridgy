"""Task queue handlers.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import bz2
import calendar
import datetime
import gc
import json
import logging
import random

from google.appengine.api import memcache
from google.appengine.api.datastore_types import _MAX_STRING_LENGTH
from google.appengine.ext import ndb
import webapp2
from webmentiontools import send

import appengine_config

from activitystreams.oauth_dropins import handlers
from activitystreams.source import Source
# need to import model class definitions since poll creates and saves entities.
import blogger
import facebook
import googleplus
import instagram
import models
from models import Response
import original_post_discovery
import tumblr
import twitter
import util
import wordpress_rest

WEBMENTION_DISCOVERY_CACHE_TIME = 60 * 60 * 24  # a day

# allows injecting timestamps in task_test.py
now_fn = datetime.datetime.now


def get_webmention_targets(source, activity):
  """Returns a set of string target URLs to attempt to send webmentions to.

  Side effect: runs the original post discovery algorithm on the activity and
  adds the resulting URLs to the activity as tags, in place.

  Args:
   source: models.Source subclass
   activity: activity dict
  """
  original_post_discovery.discover(source, activity)

  obj = activity.get('object') or activity
  urls = []

  for tag in obj.get('tags', []):
    url = tag.get('url')
    if url and tag.get('objectType') == 'article':
      url, domain, send = util.get_webmention_target(url)
      tag['url'] = url
      if send:
        urls.append(url)

  for url in obj.get('upstreamDuplicates', []):
    url, domain, send = util.get_webmention_target(url)
    if send:
      urls.append(url)

  return util.dedupe_urls(urls)


class Poll(webapp2.RequestHandler):
  """Task handler that fetches and processes new responses from a single source.

  Request parameters:
    source_key: string key of source entity
    last_polled: timestamp, YYYY-MM-DD-HH-MM-SS

  Inserts a propagate task for each response that hasn't been seen before.
  """

  def post(self):
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

    # dict with source property names and values to update
    source.last_poll_attempt = now_fn()
    source_updates = {'last_poll_attempt': source.last_poll_attempt}
    try:
      source_updates.update(self.poll(source))
    except models.DisableSource:
      # the user deauthorized the bridgy app, so disable this source.
      # let the task complete successfully so that it's not retried.
      source_updates['status'] = 'disabled'
      logging.warning('Disabling source!')
    except:
      source_updates['status'] = 'error'
      raise
    finally:
      gc.collect()  # might help avoid hitting the instance memory limit?
      source = self.update_source(source, source_updates)

    # add new poll task. randomize task ETA to within +/- 20% to try to spread
    # out tasks and prevent thundering herds.
    task_countdown = source.poll_period().total_seconds() * random.uniform(.8, 1.2)
    util.add_poll_task(source, countdown=task_countdown)

  @ndb.transactional
  def update_source(self, source, updates):
    """Merges updated fields into the source entity.

    Args:
      updates: dict mapping source property names to updated values
    """
    source = source.key.get()
    for name, val in updates.items():
      setattr(source, name, val)
    source.put()
    return source

  def poll(self, source):
    """Actually runs the poll.

    Returns: dict of source property names and values to update (transactionally)
    """
    if source.last_activities_etag or source.last_activity_id:
      logging.debug('Using ETag %s, last activity id %s',
                    source.last_activities_etag, source.last_activity_id)
    source_updates = {}

    #
    # Step 1: fetch activities
    #
    cache = util.CacheDict()
    if source.last_activities_cache_json:
      cache.update(json.loads(source.last_activities_cache_json))

    try:
      response = source.get_activities_response(
        fetch_replies=True, fetch_likes=True, fetch_shares=True, count=50,
        etag=source.last_activities_etag, min_id=source.last_activity_id,
        cache=cache)
    except Exception, e:
      code, body = handlers.interpret_http_exception(e)
      if code == '401':
        # TODO: also interpret oauth2client.AccessTokenRefreshError with
        # {'error': 'invalid_grant'} as disabled? it can mean the user revoked
        # access. it can also mean the token expired, or they deleted their
        # account, or even other things.
        # http://code.google.com/p/google-api-python-client/issues/detail?id=187#c1
        msg = 'Unauthorized error: %s' % e
        logging.warning(msg, exc_info=True)
        raise models.DisableSource(msg)
      elif code in util.HTTP_RATE_LIMIT_CODES:
        logging.warning('Rate limited. Marking as error and finishing. %s', e)
        source_updates.update({'status': 'error', 'rate_limited': True})
        return source_updates
      else:
        raise

    activities = response.get('items', [])
    logging.info('Found %d activities', len(activities))

    # extract silo activity ids, update last_activity_id
    silo_activity_ids = set()
    last_activity_id = source.last_activity_id
    for activity in activities:
      # extract activity id and maybe replace stored last activity id
      id = activity.get('id')
      if id:
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
      source_updates['last_activity_id'] = last_activity_id
      logging.debug('Storing new last activity id: %s', last_activity_id)

    # trim cache to just the returned activity ids, so that it doesn't grow
    # without bound. (WARNING: depends on get_activities_response()'s cache key
    # format, e.g. 'PREFIX ACTIVITY_ID'!
    source_updates['last_activities_cache_json'] = json.dumps(
      {k: v for k, v in cache.items() if k.split()[-1] in silo_activity_ids})

    #
    # Step 2: extract responses, store their activities in response['activities']
    #
    responses = {}  # key is response id
    for activity in activities:
      if not Source.is_public(activity):
        logging.info('Skipping non-public activity %s', id)
        continue

      # extract replies, likes, reposts, and rsvps
      obj = activity.get('object') or activity
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

        existing = responses.get(id)
        if existing:
          for field in 'url', 'content':
            old = existing.get(field)
            new = resp.get(field)
            if old != new:
              logging.error('Response %s changed %s! %s => %s', id, field, old, new)
          resp = existing
        else:
          responses[id] = resp
        resp.setdefault('activities', []).append(activity)

    #
    # Step 3: filter out responses we've already seen
    #
    # seen responses (JSON objects) for each source are stored in its entity.
    unchanged_responses = []
    if source.seen_responses_cache_json:
      for seen in json.loads(source.seen_responses_cache_json):
        id = seen['id']
        resp = responses.get(id)
        if resp and not source.as_source.activity_changed(seen, resp, log=True):
          unchanged_responses.append(seen)
          del responses[id]

    #
    # Step 4: store new responses and enqueue propagate tasks
    #
    for id, resp in responses.items():
      activities = resp.pop('activities', [])
      too_long = set()
      urls_to_activity = {}
      for i, activity in enumerate(activities):
        # we'll usually have multiple responses for the same activity, and the
        # objects in resp['activities'] are shared, so cache each activity's
        # discovered webmention targets inside its object.
        targets = activity.get('targets')
        if targets is None:
          targets = activity['targets'] = get_webmention_targets(source, activity)
          source_updates['last_syndication_url'] = source.last_syndication_url
        logging.info('%s has %d original post URL(s): %s', activity.get('url'),
                     len(targets), ' '.join(targets))
        for t in targets:
          if len(t) <= _MAX_STRING_LENGTH:
            urls_to_activity[t] = i
          else:
            logging.warning('Giving up on target URL over 500 chars! %s', t)
            too_long.add(t[:_MAX_STRING_LENGTH - 4] + '...')

      resp = Response(
        id=id,
        source=source.key,
        activities_json=[json.dumps(util.prune_activity(a)) for a in activities],
        response_json=json.dumps(resp),
        type=Response.get_type(resp),
        unsent=list(urls_to_activity.keys()),
        failed=list(too_long))
      if urls_to_activity and len(activities) > 1:
        resp.urls_to_activity=json.dumps(urls_to_activity)
      resp.get_or_save(source)

    # update cache
    if responses:
      source_updates['seen_responses_cache_json'] = json.dumps(
        responses.values() + unchanged_responses)

    source_updates.update({'last_polled': source.last_poll_attempt,
                           'status': 'enabled'})
    etag = response.get('etag')
    if etag and etag != source.last_activities_etag:
      logging.debug('Storing new ETag: %s', etag)
      source_updates['last_activities_etag'] = etag

    #
    # Step 5. possibly refetch updated syndication urls
    #
    # if the author has added syndication urls since the first time
    # original_post_discovery ran, we'll miss them. this cleanup task will
    # periodically check for updated urls. only kicks in if the author has
    # *ever* published a rel=syndication url
    if (source.last_syndication_url and
        source.last_hfeed_fetch + source.refetch_period()
            <= source.last_poll_attempt):
      self.refetch_hfeed(source)
      source_updates['last_syndication_url'] = source.last_syndication_url
      source_updates['last_hfeed_fetch'] = source.last_poll_attempt
    else:
      logging.debug(
          'skipping refetch h-feed. last-syndication-url seen: %s, '
          'last-hfeed-fetch: %s',
          source.last_syndication_url, source.last_hfeed_fetch)

    return source_updates

  def refetch_hfeed(self, source):
    """refetch and reprocess the author's url, looking for
    new or updated syndication urls that we may have missed the first
    time we looked for them.
    """
    logging.debug('refetching h-feed for source %s', source.label())
    relationships = original_post_discovery.refetch(source)
    if not relationships:
      return

    logging.debug('refetch h-feed found %d new rel=syndication relationships',
                  len(relationships))

    # grab the Responses and see if any of them have a a syndication
    # url matching one of the newly discovered relationships. We'll
    # check each response until we've seen all of them or until
    # the 60s timer runs out.
    # TODO maybe add a (canonicalized) url field to Response so we can
    # query by it instead of iterating over all of them
    for response in (Response.query(Response.source == source.key)
                     .order(-Response.created)):
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

        activity_url = source.canonicalize_syndication_url(activity_url)
        # look for activity url in the newly discovered list of relationships
        for relationship in relationships.get(activity_url, []):
          # won't re-propagate if the discovered link is already among
          # these well-known upstream duplicates
          if relationship.original in response.sent:
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

  ERROR_HTTP_RETURN_CODE = 306  # "Unused"

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
    unsent = set()
    for url in self.entity.unsent + self.entity.error + self.entity.failed:
      # recheck the url here since the checks may have failed during the poll
      # or streaming add.
      url, domain, ok = util.get_webmention_target(url)
      if ok:
        # When debugging locally, redirect our own webmentions to localhost
        if appengine_config.DEBUG and domain in util.LOCALHOST_TEST_DOMAINS:
            url = url.replace(domain, 'localhost')
        unsent.add(url)
    self.entity.unsent = sorted(unsent)
    self.entity.error = []
    self.entity.failed = []

    while self.entity.unsent:
      target = self.entity.unsent.pop(0)
      source_url = self.source_url(target)
      logging.info('Webmention from %s to %s', source_url, target)

      # see if we've cached webmention discovery for this domain. the cache
      # value is a string URL endpoint if discovery succeeded, a
      # WebmentionSend error dict if it failed (semi-)permanently, or None.
      domain = util.domain_from_link(target)
      cache_key = 'W ' + domain
      cached = memcache.get(cache_key)
      if cached:
        logging.info('Using cached webmention endpoint for %s: %s',
                     domain, cached)

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

      if error is None:
        logging.info('Sent! %s', mention.response)
        self.record_source_webmention(mention)
        self.entity.sent.append(target)
        memcache.set(cache_key, mention.receiver_endpoint,
                     time=WEBMENTION_DISCOVERY_CACHE_TIME)
      else:
        code = error['code']
        status = error.get('http_status', 0)
        if (code == 'NO_ENDPOINT' or
            (code == 'BAD_TARGET_URL' and status == 204)):  # 204 is No Content
          logging.info('Giving up this target. %s', error)
          self.entity.skipped.append(target)
          if code == 'NO_ENDPOINT':
            memcache.set(cache_key, error, time=WEBMENTION_DISCOVERY_CACHE_TIME)
        elif code in ('BAD_TARGET_URL', 'RECEIVER_ERROR') and status / 100 == 4:
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
    elif self.entity.status == 'processing' and now_fn() < self.entity.leased_until:
      self.fail('duplicate task is currently processing!')
    else:
      assert self.entity.status in ('new', 'processing', 'error')
      self.entity.status = 'processing'
      self.entity.leased_until = now_fn() + self.LEASE_LENGTH
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
      self.fail('went backward from processing to new!',
                level=logging.ERROR)

    assert self.entity.status == 'processing'
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
    self.error(self.ERROR_HTTP_RETURN_CODE)
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
    self.source.last_webmention_sent = now_fn()

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
    # TEMPORARILY skip old responses due to #374. TODO(ryan): revert
    elif datetime.datetime.now() - self.entity.created > datetime.timedelta(days=2):
      logging.warning('Dropping dupe webmention caused by issue #374')
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
          logging.error('activities: %s', self.activities)
          logging.error('urls_to_activity: %s', urls_to_activity)
          raise

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

    return '%s/%s/%s/%s/%s/%s' % (
      host_url, self.entity.type, self.entity.source.get().SHORT_NAME,
      self.entity.source.string_id(), post_id, response_id)


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
    ('/_ah/queue/poll', Poll),
    ('/_ah/queue/propagate', PropagateResponse),
    ('/_ah/queue/propagate-blogpost', PropagateBlogPost),
    ], debug=appengine_config.DEBUG)
