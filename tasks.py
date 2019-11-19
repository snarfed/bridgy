# coding=utf-8
"""Task queue handlers.
"""
from __future__ import absolute_import, division, unicode_literals
from builtins import str
from past.utils import old_div
import datetime
import gc
import logging
import random

from google.appengine.ext import ndb
from google.appengine.ext.ndb.model import _MAX_STRING_LENGTH
from granary.source import Source
from oauth_dropins.webutil import logs
from oauth_dropins.webutil.util import json_dumps, json_loads
import webapp2
from webmentiontools import send

import appengine_config

import models
from models import Response
import original_post_discovery
import util
# need to import model class definitions since poll creates and saves entities.
import blogger, facebook, flickr, github, instagram, mastodon, medium, tumblr, twitter, wordpress_rest


class Poll(webapp2.RequestHandler):
  """Task handler that fetches and processes new responses from a single source.

  Request parameters:

  * source_key: string key of source entity
  * last_polled: timestamp, YYYY-MM-DD-HH-MM-SS

  Inserts a propagate task for each response that hasn't been seen before.

  Steps:
  1: Fetch activities: posts by the user, links to the user's domain(s).
  2: Extract responses, store their activities.
  3: Filter out responses we've already seen, using Responses in the datastore.
  4: Store new responses and enqueue propagate tasks.
  5: Possibly refetch updated syndication urls.

  1-4 are in backfeed(); 5 is in poll().
  """
  RESTART_EXISTING_TASKS = False  # overridden in Discover

  def _last_poll_url(self, source):
    return '%s/%s' % (util.host_url(self),
                      logs.url(source.last_poll_attempt, source.key))

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

    logging.info('Last poll: %s', self._last_poll_url(source))

    # mark this source as polling
    source.updates = {
      'poll_status': 'polling',
      'last_poll_attempt': util.now_fn(),
      'rate_limited': False,
    }
    source = models.Source.put_updates(source)

    source.updates = {}
    try:
      self.poll(source)
    except Exception as e:
      source.updates['poll_status'] = 'error'
      code, body = util.interpret_http_exception(e)
      if code in source.DISABLE_HTTP_CODES or isinstance(e, models.DisableSource):
        # the user deauthorized the bridgy app, so disable this source.
        # let the task complete successfully so that it's not retried.
        logging.warning('Disabling source due to: %s' % e, exc_info=True)
        source.updates.update({
          'status': 'disabled',
          'poll_status': 'ok',
        })
        body = '%s\nLast poll: %s' % (source.bridgy_url(self),
                                      self._last_poll_url(source))
      elif code in source.RATE_LIMIT_HTTP_CODES:
        logging.info('Rate limited. Marking as error and finishing. %s', e)
        source.updates['rate_limited'] = True
      elif ((code and int(code) // 100 == 5) or
            code in source.TRANSIENT_ERROR_HTTP_CODES or
            util.is_connection_failure(e)):
        logging.error('API call failed. Marking as error and finishing. %s: %s\n%s',
                      code, body, e)
        self.abort(util.ERROR_HTTP_RETURN_CODE)
      else:
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
      cache.update(json_loads(source.last_activities_cache_json))

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

    # trim cache to just the returned activity ids, so that it doesn't grow
    # without bound. (WARNING: depends on get_activities_response()'s cache key
    # format, e.g. 'PREFIX ACTIVITY_ID'!)
    source.updates['last_activities_cache_json'] = json_dumps(
      {k: v for k, v in cache.items() if k.split()[-1] in silo_activity_ids})

    self.backfeed(source, responses, activities=activities)

    source.updates.update({'last_polled': source.last_poll_attempt,
                           'poll_status': 'ok'})
    if etag and etag != source.last_activities_etag:
      source.updates['last_activities_etag'] = etag

    #
    # Possibly refetch updated syndication urls.
    #
    # if the author has added syndication urls since the first time
    # original_post_discovery ran, we'll miss them. this cleanup task will
    # periodically check for updated urls. only kicks in if the author has
    # *ever* published a rel=syndication url
    if source.should_refetch():
      logging.info('refetching h-feed for source %s', source.label())
      relationships = original_post_discovery.refetch(source)

      now = util.now_fn()
      source.updates['last_hfeed_refetch'] = now

      if relationships:
        logging.info('refetch h-feed found new rel=syndication relationships: %s',
                     relationships)
        try:
          self.repropagate_old_responses(source, relationships)
        except BaseException as e:
          if ('BadRequestError' in str(e.__class__) or
              'Timeout' in str(e.__class__) or
              util.is_connection_failure(e)):
            logging.info('Timeout while repropagating responses.', exc_info=True)
          else:
            raise
    else:
      logging.info(
          'skipping refetch h-feed. last-syndication-url %s, last-refetch %s',
          source.last_syndication_url, source.last_hfeed_refetch)

  def backfeed(self, source, responses=None, activities=None):
    """Processes responses and activities and generates propagate tasks.

    Stores property names and values to update in source.updates.

    Args:
      source: Source
      responses: dict mapping AS response id to AS object
      activities: dict mapping AS activity id to AS object
    """
    if responses is None:
      responses = {}
    if activities is None:
      activities = {}

    # Cache to make sure we only fetch the author's h-feed(s) the
    # first time we see it
    fetched_hfeeds = set()

    # narrow down to just public activities
    public = {}
    private = {}
    for id, activity in activities.items():
      (public if source.is_activity_public(activity) else private)[id] = activity
    logging.info('Found %d public activities: %s', len(public), public.keys())
    logging.info('Found %d private activities: %s', len(private), private.keys())

    last_public_post = (source.last_public_post or util.EPOCH).isoformat()
    public_published = util.trim_nulls([a.get('published') for a in public.values()])
    if public_published:
      max_published = max(public_published)
      if max_published > last_public_post:
        last_public_post = max_published
        source.updates['last_public_post'] = \
          util.as_utc(util.parse_iso8601(max_published))

    source.updates['recent_private_posts'] = \
      len([a for a in private.values()
           if a.get('published', util.EPOCH_ISO) > last_public_post])

    #
    # Step 2: extract responses, store their activities in response['activities']
    #
    # WARNING: this creates circular references in link posts found by search
    # queries in step 1, since they are their own activity. We use
    # prune_activity() and prune_response() in step 4 to remove these before
    # serializing to JSON.
    #
    for id, activity in public.items():
      obj = activity.get('object') or activity

      # handle user mentions
      user_id = source.user_tag_id()
      if obj.get('author', {}).get('id') != user_id and activity.get('verb') != 'share':
        for tag in obj.get('tags', []):
          urls = tag.get('urls')
          if tag.get('objectType') == 'person' and tag.get('id') == user_id and urls:
            activity['originals'], activity['mentions'] = \
              original_post_discovery.discover(
                source, activity, fetch_hfeed=True,
                include_redirect_sources=False,
                already_fetched_hfeeds=fetched_hfeeds)
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
                source, activity, fetch_hfeed=True,
                include_redirect_sources=False,
                already_fetched_hfeeds=fetched_hfeeds)
          responses[id] = activity
          break

      # extract replies, likes, reactions, reposts, and rsvps
      replies = obj.get('replies', {}).get('items', [])
      tags = obj.get('tags', [])
      likes = [t for t in tags if Response.get_type(t) == 'like']
      reactions = [t for t in tags if Response.get_type(t) == 'react']
      reposts = [t for t in tags if Response.get_type(t) == 'repost']
      rsvps = Source.get_rsvps_from_event(obj)

      # coalesce responses. drop any without ids
      for resp in replies + likes + reactions + reposts + rsvps:
        id = resp.get('id')
        if not id:
          logging.error('Skipping response without id: %s', json_dumps(resp, indent=2))
          continue

        if source.is_blocked(resp):
          logging.info('Skipping response by blocked user: %s',
                       json_dumps(resp.get('author') or resp.get('actor'), indent=2))
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
      for seen in json_loads(source.seen_responses_cache_json):
        id = seen['id']
        resp = responses.get(id)
        if resp and not source.gr_source.activity_changed(seen, resp, log=True):
          unchanged_responses.append(seen)
          del responses[id]

    #
    # Step 4: store new responses and enqueue propagate tasks
    #
    pruned_responses = []
    source.blocked_ids = None

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
              source, activity, fetch_hfeed=True,
              include_redirect_sources=False,
              already_fetched_hfeeds=fetched_hfeeds)

        targets = original_post_discovery.targets_for_response(
          resp, originals=activity['originals'], mentions=activity['mentions'])
        if targets:
          logging.info('%s has %d webmention target(s): %s', activity.get('url'),
                       len(targets), ' '.join(targets))
          # new response to propagate! load block list if we haven't already
          if source.blocked_ids is None:
            source.load_blocklist()

        for t in targets:
          if len(t) <= _MAX_STRING_LENGTH:
            urls_to_activity[t] = i
          else:
            logging.info('Giving up on target URL over %s chars! %s',
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
        activities_json=[json_dumps(util.prune_activity(a, source))
                         for a in activities],
        response_json=json_dumps(pruned_response),
        type=resp_type,
        unsent=list(urls_to_activity.keys()),
        failed=list(too_long),
        original_posts=resp.get('originals', []))
      if urls_to_activity and len(activities) > 1:
        resp_entity.urls_to_activity=json_dumps(urls_to_activity)
      resp_entity.get_or_save(source, restart=self.RESTART_EXISTING_TASKS)

    # update cache
    if pruned_responses:
      source.updates['seen_responses_cache_json'] = json_dumps(
        pruned_responses + unchanged_responses)

  def repropagate_old_responses(self, source, relationships):
    """Find old Responses that match a new SyndicatedPost and repropagate them.

    We look through as many responses as we can until the datastore query expires.

    Args:
      source: :class:`models.Source`
      relationships: refetch result
    """
    for response in (Response.query(Response.source == source.key)
                     .order(-Response.updated)):
      new_orig_urls = set()
      for activity_json in response.activities_json:
        activity = json_loads(activity_json)
        activity_url = activity.get('url') or activity.get('object', {}).get('url')
        if not activity_url:
          logging.warning('activity has no url %s', activity_json)
          continue

        activity_url = source.canonicalize_url(activity_url, activity=activity)
        if not activity_url:
          continue

        # look for activity url in the newly discovered list of relationships
        for relationship in relationships.get(activity_url, []):
          # won't re-propagate if the discovered link is already among
          # these well-known upstream duplicates
          if (relationship.original in response.sent or
              relationship.original in response.original_posts):
            logging.info(
              '%s found a new rel=syndication link %s -> %s, but the '
              'relationship had already been discovered by another method',
              response.label(), relationship.original, relationship.syndication)
          else:
            logging.info(
              '%s found a new rel=syndication link %s -> %s, and '
              'will be repropagated with a new target!',
              response.label(), relationship.original, relationship.syndication)
            new_orig_urls.add(relationship.original)

      if new_orig_urls:
        # re-open a previously 'complete' propagate task
        response.status = 'new'
        response.unsent.extend(list(new_orig_urls))
        response.put()
        response.add_task()


class Discover(Poll):
  """Task handler that fetches and processes new responses to a single post.

  Request parameters:

  * source_key: string key of source entity
  * post_id: string, silo post id(s)

  Inserts a propagate task for each response that hasn't been seen before.

  Original feature request: https://github.com/snarfed/bridgy/issues/579
  """
  RESTART_EXISTING_TASKS = True

  def post(self):
    logging.debug('Params: %s', self.request.params)

    type = self.request.get('type')
    if type:
      assert type in ('event',)

    source = util.load_source(self)
    if not source or source.status == 'disabled' or 'listen' not in source.features:
      logging.error('Source not found or disabled. Dropping task.')
      return
    logging.info('Source: %s %s, %s', source.label(), source.key.string_id(),
                 source.bridgy_url(self))

    post_id = util.get_required_param(self, 'post_id')
    source.updates = {}

    try:
      if type == 'event':
        activities = [source.gr_source.get_event(post_id)]
      else:
        activities = source.get_activities(
          fetch_replies=True, fetch_likes=True, fetch_shares=True,
          activity_id=post_id, user_id=source.key.id())

      if not activities or not activities[0]:
        logging.info('Post %s not found.', post_id)
        return
      assert len(activities) == 1, activities
      self.backfeed(source, activities={activities[0]['id']: activities[0]})

      obj = activities[0].get('object') or activities[0]
      in_reply_to = util.get_first(obj, 'inReplyTo')
      if in_reply_to:
        parsed = util.parse_tag_uri(in_reply_to.get('id', ''))  # TODO: fall back to url
        if parsed:
          util.add_discover_task(source, parsed[1])

    except Exception as e:
      code, body = util.interpret_http_exception(e)
      if (code and (code in source.RATE_LIMIT_HTTP_CODES or
                    code in ('400', '404') or
                    int(code) // 100 == 5)
            or util.is_connection_failure(e)):
        logging.error('API call failed; giving up. %s: %s\n%s', code, body, e)
        self.abort(util.ERROR_HTTP_RETURN_CODE)
      else:
        raise


class SendWebmentions(webapp2.RequestHandler):
  """Abstract base task handler that can send webmentions.

  Attributes:

  * entity: :class:`models.Webmentions` subclass instance (set in :meth:`lease_entity`)
  * source: :class:`models.Source` entity (set in :meth:`send_webmentions`)
  """

  # request deadline (10m) plus some padding
  LEASE_LENGTH = datetime.timedelta(minutes=12)

  def source_url(self, target_url):
    """Return the source URL to use for a given target URL.

    Subclasses must implement.

    Args:
      target_url: string

    Returns:
      string
    """
    raise NotImplementedError()

  def send_webmentions(self):
    """Tries to send each unsent webmention in self.entity.

    Uses :meth:`source_url()` to determine the source parameter for each
    webmention.

    :meth:`lease()` *must* be called before this!
    """
    logging.info('Starting %s', self.entity.label())

    self.source = self.entity.source.get()
    try:
      self.do_send_webmentions()
    except:
      logging.info('Propagate task failed', exc_info=True)
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
          logging.info('Giving up on target URL over %s chars! %s',
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
      cached = util.webmention_endpoint_cache.get(cache_key)
      if cached:
        logging.info('Using cached webmention endpoint %r: %s', cache_key, cached)

      # send! and handle response or error
      error = None
      if isinstance(cached, dict):
        error = cached
      else:
        mention = send.WebmentionSend(source_url, target, endpoint=cached)
        headers = util.request_headers(source=self.source)
        logging.info('Sending...')
        try:
          if not mention.send(timeout=999, headers=headers):
            error = mention.error
        except BaseException as e:
          logging.info('', exc_info=True)
          error = getattr(mention, 'error')
          if not error:
            error = ({'code': 'BAD_TARGET_URL', 'http_status': 499}
                     if 'DNS lookup failed for URL:' in str(e)
                     else {'code': 'EXCEPTION'})

      error_code = error['code'] if error else None
      if error_code != 'BAD_TARGET_URL' and not cached:
        val = error if error_code == 'NO_ENDPOINT' else mention.receiver_endpoint
        with util.webmention_endpoint_cache_lock:
          util.webmention_endpoint_cache[cache_key] = val

      if error is None:
        logging.info('Sent! %s', mention.response)
        self.record_source_webmention(mention)
        self.entity.sent.append(target)
      else:
        status = error.get('http_status', 0)
        if (error_code == 'NO_ENDPOINT' or
            (error_code == 'BAD_TARGET_URL' and status == 204)):  # No Content
          logging.info('Giving up this target. %s', error)
          self.entity.skipped.append(target)
        elif status // 100 == 4:
          # Give up on 4XX errors; we don't expect later retries to succeed.
          logging.info('Giving up this target. %s', error)
          self.entity.failed.append(target)
        else:
          self.fail('Error sending to endpoint: %s' % error, level=logging.INFO)
          self.entity.error.append(target)

      if target in self.entity.unsent:
        self.entity.unsent.remove(target)

    if self.entity.error:
      logging.info('Propagate task failed')
      self.release('error')
    else:
      self.complete()

  @ndb.transactional
  def lease(self, key):
    """Attempts to acquire and lease the :class:`models.Webmentions` entity.

    Returns True on success, False or None otherwise.

    TODO: unify with :meth:`complete()`

    Args:
      key: :class:`ndb.Key`
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
    """Attempts to mark the :class:`models.Webmentions` entity completed.

    Returns True on success, False otherwise.
    """
    existing = self.entity.key.get()
    if existing is None:
      self.fail('entity disappeared!', level=logging.ERROR)
    elif existing.status == 'complete':
      # let this task return 200 and finish
      logging.warning('another task stole and finished this. did my lease expire?')
    elif self.entity.status == 'complete':
      # let this task return 200 and finish
      logging.error('i already completed this task myself somehow?! '
                    'https://github.com/snarfed/bridgy/issues/610')
    elif existing.status == 'new':
      self.fail('went backward from processing to new!', level=logging.ERROR)
    else:
      assert existing.status == 'processing', existing.status
      assert self.entity.status == 'processing', self.entity.status
      self.entity.status = 'complete'
      self.entity.put()
      return True

    return False

  @ndb.transactional
  def release(self, new_status):
    """Attempts to unlease the :class:`models.Webmentions` entity.

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
    self.error(util.ERROR_HTTP_RETURN_CODE)
    logging.log(level, message)
    self.response.out.write(message)

  @ndb.transactional
  def record_source_webmention(self, mention):
    """Sets this source's last_webmention_sent and maybe webmention_endpoint.

    Args:
      mention: :class:`webmentiontools.send.WebmentionSend`
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
  """Task handler that sends webmentions for a :class:`models.Response`.

  Attributes:

  * activities: parsed :attr:`models.Response.activities_json` list

  Request parameters:

  * response_key: string key of :class:`models.Response` entity
  """

  def post(self):
    logging.debug('Params: %s', self.request.params)
    if not self.lease(ndb.Key(urlsafe=self.request.params['response_key'])):
      return

    source = self.entity.source.get()
    if not source:
      logging.warning('Source not found! Dropping response.')
      return
    logging.info('Source: %s %s, %s', source.label(), source.key.string_id(),
                 source.bridgy_url(self))
    poll_estimate = self.entity.created - datetime.timedelta(seconds=61)
    logging.info('Created by this poll: %s/%s', util.host_url(self),
                 logs.url(poll_estimate, source.key))

    self.activities = [json_loads(a) for a in self.entity.activities_json]
    response_obj = json_loads(self.entity.response_json)
    if (not source.is_activity_public(response_obj) or
        not all(source.is_activity_public(a) for a in self.activities)):
      logging.info('Response or activity is non-public. Dropping.')
      self.complete()
      return

    self.send_webmentions()

  def source_url(self, target_url):
    # determine which activity to use
    try:
      activity = self.activities[0]
      if self.entity.urls_to_activity:
        urls_to_activity = json_loads(self.entity.urls_to_activity)
        if urls_to_activity:
          activity = self.activities[urls_to_activity[target_url]]
    except (KeyError, IndexError):
      logging.warning("""\
Hit https://github.com/snarfed/bridgy/issues/237 KeyError!
target url %s not in urls_to_activity: %s
activities: %s""", target_url, self.entity.urls_to_activity, self.activities)
      self.abort(util.ERROR_HTTP_RETURN_CODE)

    # generate source URL
    id = activity['id']
    parsed = util.parse_tag_uri(id)
    post_id = parsed[1] if parsed else id
    # prefer brid-gy.appspot.com to brid.gy because non-browsers (ie OpenSSL)
    # currently have problems with brid.gy's SSL cert. details:
    # https://github.com/snarfed/bridgy/issues/20
    host_url = self.request.host_url
    domain = util.domain_from_link(host_url)
    if domain == util.PRIMARY_DOMAIN or domain in util.OTHER_DOMAINS:
      host_url = 'https://brid-gy.appspot.com'

    path = [host_url, self.entity.type, self.entity.source.get().SHORT_NAME,
            self.entity.source.string_id(), post_id]

    if self.entity.type != 'post':
      # parse and add response id. (we know Response key ids are always tag URIs)
      _, response_id = util.parse_tag_uri(self.entity.key.string_id())
      reaction_id = response_id
      if self.entity.type in ('like', 'react', 'repost', 'rsvp'):
        response_id = response_id.split('_')[-1]  # extract responder user id
      path.append(response_id)
      if self.entity.type == 'react':
        path.append(reaction_id)

    return '/'.join(path)


class PropagateBlogPost(SendWebmentions):
  """Task handler that sends webmentions for a :class:`models.BlogPost`.

  Request parameters:

  * key: string key of :class:`models.BlogPost` entity
  """

  def post(self):
    logging.debug('Params: %s', self.request.params)

    if not self.lease(ndb.Key(urlsafe=self.request.params['key'])):
      return

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
    ('/_ah/queue/discover', Discover),
    ('/_ah/queue/propagate', PropagateResponse),
    ('/_ah/queue/propagate-blogpost', PropagateBlogPost),
    ], debug=appengine_config.DEBUG)
