# coding=utf-8
"""Task queue handlers.
"""
import datetime
import gc
import logging

from flask import g, request
from flask.views import View
from google.cloud import ndb
from google.cloud.ndb._datastore_types import _MAX_STRING_LENGTH
from granary.source import Source
from oauth_dropins.webutil import logs, webmention
from oauth_dropins.webutil.flask_util import error
from oauth_dropins.webutil.util import json_dumps, json_loads

import models, original_post_discovery, util
from flask_background import app
from models import Response
from util import ERROR_HTTP_RETURN_CODE
# need to import model class definitions since poll creates and saves entities.
import blogger, facebook, flickr, github, instagram, mastodon, medium, reddit, tumblr, twitter, wordpress_rest

logger = logging.getLogger(__name__)

# Used as a sentinel value in the webmention endpoint cache
NO_ENDPOINT = 'NONE'


# TODO: move into granary.microformats2?
def is_quote_mention(activity, source):
  obj = activity.get('object') or activity
  for att in obj.get('attachments', []):
    if (att.get('objectType') in ('note', 'article')
        and att.get('author', {}).get('id') == source.user_tag_id()):
      return True


class Poll(View):
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
    return util.host_url(logs.url(source.last_poll_attempt, source.key))

  def dispatch_request(self):
    logger.debug(f'Params: {list(request.values.items())}')

    key = request.values['source_key']
    source = g.source = ndb.Key(urlsafe=key).get()
    if not source or source.status == 'disabled' or 'listen' not in source.features:
      logger.error('Source not found or disabled. Dropping task.')
      return ''
    logger.info(f'Source: {source.label()} {source.key_id()}, {source.bridgy_url()}')

    if source.AUTO_POLL:
      last_polled = request.values['last_polled']
      if last_polled != source.last_polled.strftime(util.POLL_TASK_DATETIME_FORMAT):
        logger.warning('duplicate poll task! deferring to the other task.')
        return ''

    logger.info(f'Last poll: {self._last_poll_url(source)}')

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
      code, _ = util.interpret_http_exception(e)
      if code in source.DISABLE_HTTP_CODES or isinstance(e, models.DisableSource):
        # the user deauthorized the bridgy app, so disable this source.
        # let the task complete successfully so that it's not retried.
        logger.warning(f'Disabling source due to: {e}', exc_info=True)
        source.updates.update({
          'status': 'disabled',
          'poll_status': 'ok',
        })
      elif code in source.RATE_LIMIT_HTTP_CODES:
        logger.info(f'Rate limited. Marking as error and finishing. {e}')
        source.updates['rate_limited'] = True
      else:
        raise
    finally:
      source = models.Source.put_updates(source)

    if source.AUTO_POLL:
      util.add_poll_task(source)

    # feeble attempt to avoid hitting the instance memory limit
    source = None
    gc.collect()

    return 'OK'

  def poll(self, source):
    """Actually runs the poll.

    Stores property names and values to update in source.updates.
    """
    if source.last_activities_etag or source.last_activity_id:
      logger.debug(f'Using ETag {source.last_activities_etag}, last activity id {source.last_activity_id}')

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

    # these map ids to AS objects.
    # backfeed all links as responses, but only include the user's own links as
    # activities, since their responses also get backfeed.
    responses = {a['id']: a for a in links}

    user_id = source.user_tag_id()
    links_by_user = [a for a in links
                     if a.get('object', {}).get('author', {}).get('id') == user_id]
    activities = {a['id']: a for a in links_by_user + user_activities}

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
        greater = str(id) > str(last_activity_id)
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
      logger.info(f'refetching h-feed for source {source.label()}')
      relationships = original_post_discovery.refetch(source)

      now = util.now_fn()
      source.updates['last_hfeed_refetch'] = now

      if relationships:
        logger.info(f'refetch h-feed found new rel=syndication relationships: {relationships}')
        try:
          self.repropagate_old_responses(source, relationships)
        except BaseException as e:
          if ('BadRequestError' in str(e.__class__) or
              'Timeout' in str(e.__class__) or
              util.is_connection_failure(e)):
            logger.info('Timeout while repropagating responses.', exc_info=True)
          else:
            raise
    else:
      logger.info(
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
    logger.info(f'Found {len(public)} public activities: {public.keys()}')
    logger.info(f'Found {len(private)} private activities: {private.keys()}')

    last_public_post = (source.last_public_post or util.EPOCH).isoformat()
    public_published = util.trim_nulls(
      [a.get('object', {}).get('published') for a in public.values()])
    if public_published:
      max_published = max(public_published)
      if max_published > last_public_post:
        last_public_post = max_published
        source.updates['last_public_post'] = \
          util.as_utc(util.parse_iso8601(max_published))

    source.updates['recent_private_posts'] = \
      len([a for a in private.values()
           if a.get('object', {}).get('published', util.EPOCH_ISO) > last_public_post])

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
      if is_quote_mention(activity, source):
        # now that we've confirmed that one exists, OPD will dig
        # into the actual attachments
        if 'originals' not in activity or 'mentions' not in activity:
          activity['originals'], activity['mentions'] = \
            original_post_discovery.discover(
              source, activity, fetch_hfeed=True,
              include_redirect_sources=False,
              already_fetched_hfeeds=fetched_hfeeds)
        responses[id] = activity

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
          logger.error(f'Skipping response without id: {json_dumps(resp, indent=2)}')
          continue

        if source.is_blocked(resp):
          dump = json_dumps(resp.get('author') or resp.get('actor'), indent=2)
          logger.info(f'Skipping response by blocked user: {dump}')
          continue

        resp.setdefault('activities', []).append(activity)

        # when we find two responses with the same id, the earlier one may have
        # come from a link post or user mention, and this one is probably better
        # since it probably came from the user's activity, so prefer this one.
        # background: https://github.com/snarfed/bridgy/issues/533
        existing = responses.get(id)
        if existing:
          if source.gr_source.activity_changed(resp, existing, log=True):
            logger.warning(f'Got two different versions of same response!\n{existing}\n{resp}')
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
      if not activities and (resp_type == 'post' or is_quote_mention(resp)):
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
          logger.info(f"{activity.get('url')} has {len(targets)} webmention target(s): {' '.join(targets)}")
          # new response to propagate! load block list if we haven't already
          if source.blocked_ids is None:
            source.load_blocklist()

        for t in targets:
          if len(t) <= _MAX_STRING_LENGTH:
            urls_to_activity[t] = i
          else:
            logger.info(f'Giving up on target URL over {_MAX_STRING_LENGTH} chars! {t}')
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
          logger.warning(f'activity has no url {activity_json}')
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
            logger.info(
              '%s found a new rel=syndication link %s -> %s, but the '
              'relationship had already been discovered by another method',
              response.label(), relationship.original, relationship.syndication)
          else:
            logger.info(
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

  def dispatch_request(self):
    logger.debug(f'Params: {list(request.values.items())}')
    g.TRANSIENT_ERROR_HTTP_CODES = ('400', '404')

    type = request.values.get('type')
    if type:
      assert type in ('event',)

    source = g.source = util.load_source()
    if not source or source.status == 'disabled' or 'listen' not in source.features:
      logger.error('Source not found or disabled. Dropping task.')
      return ''
    logger.info(f'Source: {source.label()} {source.key_id()}, {source.bridgy_url()}')

    post_id = request.values['post_id']
    source.updates = {}

    if type == 'event':
      activities = [source.gr_source.get_event(post_id)]
    else:
      activities = source.get_activities(
        fetch_replies=True, fetch_likes=True, fetch_shares=True,
        activity_id=post_id, user_id=source.key_id())

    if not activities or not activities[0]:
      logger.info(f'Post {post_id} not found.')
      return ''
    assert len(activities) == 1, activities
    activity = activities[0]
    activities = {activity['id']: activity}
    self.backfeed(source, responses=activities, activities=activities)

    obj = activity.get('object') or activity
    in_reply_to = util.get_first(obj, 'inReplyTo')
    if in_reply_to:
      parsed = util.parse_tag_uri(in_reply_to.get('id', ''))  # TODO: fall back to url
      if parsed:
        util.add_discover_task(source, parsed[1])

    return 'OK'


class SendWebmentions(View):
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
    logger.info(f'Starting {self.entity.label()}')

    try:
      self.do_send_webmentions()
    except:
      logger.info('Propagate task failed', exc_info=True)
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
          logger.info(f'Giving up on target URL over {_MAX_STRING_LENGTH} chars! {url}')
          self.entity.failed.append(orig_url)
    self.entity.unsent = sorted(unsent)

    while self.entity.unsent:
      resp = None
      target = self.entity.unsent.pop(0)

      try:
        source_url = self.source_url(target)
        logger.info(f'Webmention from {source_url} to {target}')

        # see if we've cached webmention discovery for this domain. the cache
        # value is a string URL endpoint if discovery succeeded, NO_ENDPOINT if
        # no endpoint was ofund.
        cache_key = util.webmention_endpoint_cache_key(target)
        endpoint = util.webmention_endpoint_cache.get(cache_key)
        if endpoint:
          logger.info(f'Webmention discovery: using cached endpoint {cache_key}: {endpoint}')

        # send! and handle response or error
        headers = util.request_headers(source=g.source)
        if not endpoint:
          endpoint, resp = webmention.discover(target, headers=headers)
          with util.webmention_endpoint_cache_lock:
            util.webmention_endpoint_cache[cache_key] = endpoint or NO_ENDPOINT

        if endpoint and endpoint != NO_ENDPOINT:
          logger.info('Sending...')
          resp = webmention.send(endpoint, source_url, target, timeout=999,
                                 headers=headers)
          logger.info(f'Sent! {resp}')
          self.record_source_webmention(endpoint, target)
          self.entity.sent.append(target)
        else:
          logger.info('Giving up this target.')
          self.entity.skipped.append(target)

      except ValueError:
        logger.info('Bad URL; giving up this target.')
        self.entity.skipped.append(target)

      except BaseException as e:
        logger.info('', exc_info=True)
        # Give up on 4XX and DNS errors; we don't expect retries to succeed.
        code, _ = util.interpret_http_exception(e)
        if (code and code.startswith('4')) or 'DNS lookup failed' in str(e):
          logger.info('Giving up this target.')
          self.entity.failed.append(target)
        else:
          self.fail(f'Error sending to endpoint: {resp}')
          self.entity.error.append(target)

      if target in self.entity.unsent:
        self.entity.unsent.remove(target)

    if self.entity.error:
      logger.info('Some targets failed')
      self.release('error')
    else:
      self.complete()

  @ndb.transactional()
  def lease(self, key):
    """Attempts to acquire and lease the :class:`models.Webmentions` entity.

    Also loads and sets `g.source`, and returns False if the source doesn't
    exist or is disabled.

    TODO: unify with :meth:`complete()`

    Args:
      key: :class:`ndb.Key`

    Returns: True on success, False or None otherwise
    """
    self.entity = key.get()

    if self.entity is None:
      return self.fail('no entity!')
    elif self.entity.status == 'complete':
      # let this task return 200 and finish
      logger.warning('duplicate task already propagated this')
      return
    elif (self.entity.status == 'processing' and
          util.now_fn() < self.entity.leased_until):
      return self.fail('duplicate task is currently processing!')

    g.source = self.entity.source.get()
    if not g.source or g.source.status == 'disabled':
      logger.error('Source not found or disabled. Dropping task.')
      return False
    logger.info(f'Source: {g.source.label()} {g.source.key_id()}, {g.source.bridgy_url()}')

    assert self.entity.status in ('new', 'processing', 'error'), self.entity.status
    self.entity.status = 'processing'
    self.entity.leased_until = util.now_fn() + self.LEASE_LENGTH
    self.entity.put()
    return True

  @ndb.transactional()
  def complete(self):
    """Attempts to mark the :class:`models.Webmentions` entity completed.

    Returns True on success, False otherwise.
    """
    existing = self.entity.key.get()
    if existing is None:
      self.fail('entity disappeared!')
    elif existing.status == 'complete':
      # let this task return 200 and finish
      logger.warning('another task stole and finished this. did my lease expire?')
    elif self.entity.status == 'complete':
      # let this task return 200 and finish
      logger.error('i already completed this task myself somehow?! '
                    'https://github.com/snarfed/bridgy/issues/610')
    elif existing.status == 'new':
      self.fail('went backward from processing to new!')
    else:
      assert existing.status == 'processing', existing.status
      assert self.entity.status == 'processing', self.entity.status
      self.entity.status = 'complete'
      self.entity.put()
      return True

    return False

  @ndb.transactional()
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

  def fail(self, message):
    """Marks the request failed and logs an error message."""
    logger.warning(message)
    g.failed = True

  @ndb.transactional()
  def record_source_webmention(self, endpoint, target):
    """Sets this source's last_webmention_sent and maybe webmention_endpoint.

    Args:
      endpoint: str, URL
      target: str, URL
    """
    g.source = g.source.key.get()
    logger.info('Setting last_webmention_sent')
    g.source.last_webmention_sent = util.now_fn()

    if (endpoint != g.source.webmention_endpoint and
        util.domain_from_link(target) in g.source.domains):
      logger.info(f'Also setting webmention_endpoint to {endpoint} (discovered in {target}; was {g.source.webmention_endpoint})')
      g.source.webmention_endpoint = endpoint

    g.source.put()


class PropagateResponse(SendWebmentions):
  """Task handler that sends webmentions for a :class:`models.Response`.

  Attributes:

  * activities: parsed :attr:`models.Response.activities_json` list

  Request parameters:

  * response_key: string key of :class:`models.Response` entity
  """

  def dispatch_request(self):
    logger.debug(f'Params: {list(request.values.items())}')
    if not self.lease(ndb.Key(urlsafe=request.values['response_key'])):
      return ('', ERROR_HTTP_RETURN_CODE) if getattr(g, 'failed', None) else 'OK'

    source = g.source
    poll_estimate = self.entity.created - datetime.timedelta(seconds=61)
    poll_url = util.host_url(logs.url(poll_estimate, source.key))
    logger.info(f'Created by this poll: {poll_url}')

    self.activities = [json_loads(a) for a in self.entity.activities_json]
    response_obj = json_loads(self.entity.response_json)
    if (not source.is_activity_public(response_obj) or
        not all(source.is_activity_public(a) for a in self.activities)):
      logger.info('Response or activity is non-public. Dropping.')
      self.complete()
      return ''

    self.send_webmentions()
    return ('', ERROR_HTTP_RETURN_CODE) if getattr(g, 'failed', None) else 'OK'

  def source_url(self, target_url):
    # determine which activity to use
    try:
      activity = self.activities[0]
      if self.entity.urls_to_activity:
        urls_to_activity = json_loads(self.entity.urls_to_activity)
        if urls_to_activity:
          activity = self.activities[urls_to_activity[target_url]]
    except (KeyError, IndexError):
      error(f"""Hit https://github.com/snarfed/bridgy/issues/237 KeyError!
target url {target_url} not in urls_to_activity: {self.entity.urls_to_activity}
activities: {self.activities}""", status=ERROR_HTTP_RETURN_CODE)

    # generate source URL
    id = activity['id']
    parsed = util.parse_tag_uri(id)
    post_id = parsed[1] if parsed else id
    parts = [self.entity.type, g.source.SHORT_NAME, g.source.key.string_id(), post_id]

    if self.entity.type != 'post':
      # parse and add response id. (we know Response key ids are always tag URIs)
      _, response_id = util.parse_tag_uri(self.entity.key.string_id())
      reaction_id = response_id
      if self.entity.type in ('like', 'react', 'repost', 'rsvp'):
        response_id = response_id.split('_')[-1]  # extract responder user id
      parts.append(response_id)
      if self.entity.type == 'react':
        parts.append(reaction_id)

    return util.host_url('/'.join(parts))


class PropagateBlogPost(SendWebmentions):
  """Task handler that sends webmentions for a :class:`models.BlogPost`.

  Request parameters:

  * key: string key of :class:`models.BlogPost` entity
  """

  def dispatch_request(self):
    logger.debug(f'Params: {list(request.values.items())}')

    if not self.lease(ndb.Key(urlsafe=request.values['key'])):
      return ('', ERROR_HTTP_RETURN_CODE) if getattr(g, 'failed', None) else 'OK'

    to_send = set()
    for url in self.entity.unsent:
      url, domain, ok = util.get_webmention_target(url)
      # skip "self" links to this blog's domain
      if ok and domain not in g.source.domains:
        to_send.add(url)

    self.entity.unsent = list(to_send)
    self.send_webmentions()
    return ('', ERROR_HTTP_RETURN_CODE) if getattr(g, 'failed', None) else 'OK'

  def source_url(self, target_url):
    return self.entity.key.id()


app.add_url_rule('/_ah/queue/poll', view_func=Poll.as_view('poll'), methods=['POST'])
app.add_url_rule('/_ah/queue/poll-now', view_func=Poll.as_view('poll-now'), methods=['POST'])
app.add_url_rule('/_ah/queue/discover', view_func=Discover.as_view('discover'), methods=['POST'])
app.add_url_rule('/_ah/queue/propagate', view_func=PropagateResponse.as_view('propagate'), methods=['POST'])
app.add_url_rule('/_ah/queue/propagate-blogpost', view_func=PropagateBlogPost.as_view('propagate_blogpost'), methods=['POST'])
