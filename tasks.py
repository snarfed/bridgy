"""Task queue handlers.

TODO: cron job to find sources without seed poll tasks.
TODO: think about how to determine stopping point. can all sources return
comments in strict descending timestamp order? can we require/generate
monotonically increasing comment ids for all sources?
TODO: check HRD consistency guarantees and change as needed
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import gc
import json
import logging
import random
import urllib2

from apiclient import errors
from google.appengine.api import memcache
from google.appengine.ext import ndb
from oauth2client.client import AccessTokenRefreshError
from python_instagram.bind import InstagramAPIError
import webapp2
from webmentiontools import send

import appengine_config

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

# when running in dev_appserver, replace these domains in links with localhost
LOCALHOST_TEST_DOMAINS = frozenset(('kylewm', 'snarfed.org'))

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

  targets = set()
  obj = activity.get('object') or activity

  for tag in obj.get('tags', []):
    url = tag.get('url')
    if url and tag.get('objectType') == 'article':
      url, domain, send = util.get_webmention_target(url)
      tag['url'] = url
      if send:
        targets.add(url)

  for url in obj.get('upstreamDuplicates', []):
    url, domain, send = util.get_webmention_target(url)
    if send:
      targets.add(url)

  return targets


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
    logging.info('Source: %s %s', source.label(), source.key.string_id())

    last_polled = self.request.params['last_polled']
    if last_polled != source.last_polled.strftime(util.POLL_TASK_DATETIME_FORMAT):
      logging.warning('duplicate poll task! deferring to the other task.')
      return

    now = now_fn()
    source.last_poll_attempt = now
    # randomize task ETA to within +/- 20% to try to spread out tasks and
    # prevent thundering herds.
    task_countdown = source.poll_period().total_seconds() * random.uniform(.8, 1.2)
    try:
      self.do_post(source)
      util.add_poll_task(source, countdown=task_countdown)
    except models.DisableSource:
      # the user deauthorized the bridgy app, so disable this source.
      # let the task complete successfully so that it's not retried.
      source.status = 'disabled'
      logging.warning('Disabling source!')
    except:
      source.status = 'error'
      raise
    finally:
      gc.collect()  # might help avoid hitting the instance memory limit
      source.put()

  def do_post(self, source):
    if source.last_activities_etag or source.last_activity_id:
      logging.debug('Using ETag %s, last activity id %s',
                    source.last_activities_etag, source.last_activity_id)

    #
    # Step 1: fetch activities
    #
    try:
      response = source.get_activities_response(
        fetch_replies=True, fetch_likes=True, fetch_shares=True, count=50,
        etag=source.last_activities_etag, min_id=source.last_activity_id,
        cache=memcache)
    except Exception, e:
      # note that activitystreams-unofficial doesn't use requests (yet!), so no
      # need to catch requests.HTTPError.
      body = None
      if isinstance(e, urllib2.HTTPError):
        code = e.code
        try:
          body = e.read()
        except AttributeError:
          # no response body
          pass
      elif isinstance(e, errors.HttpError):
        code = e.resp.status
        body = e.content
      elif isinstance(e, InstagramAPIError):
        if e.error_type == 'OAuthAccessTokenException':
          code = '401'
        else:
          code = e.status_code
      elif isinstance(e, AccessTokenRefreshError) and str(e) == 'invalid_grant':
        code = '401'
      else:
        raise

      if body:
        logging.error('Error response body: %s', body)

      code = str(code)
      if code == '401':
        # TODO: also interpret oauth2client.AccessTokenRefreshError with
        # {'error': 'invalid_grant'} as disabled? it can mean the user revoked
        # access. it can also mean the token expired, or they deleted their
        # account, or even other things.
        # http://code.google.com/p/google-api-python-client/issues/detail?id=187#c1
        msg = 'Unauthorized error: %s' % e
        logging.exception(msg)
        raise models.DisableSource(msg)
      elif code in ('403', '429', '503'):
        # rate limiting errors. twitter returns 429, instagram 503, google+ 403.
        # TODO: facebook. it returns 200 and reports the error in the response.
        # https://developers.facebook.com/docs/reference/ads-api/api-rate-limiting/
        logging.warning('Rate limited. Marking as error and finishing. %s', e)
        source.status = 'error'
        return
      else:
        raise

    activities = response.get('items', [])
    logging.info('Found %d activities', len(activities))
    last_activity_id = source.last_activity_id

    #
    # Step 2: extract responses, store activity in response['activity']
    #
    responses = {}
    for activity in activities:
      # extract activity id and maybe replace stored last activity id
      id = activity.get('id')
      if id:
        parsed = util.parse_tag_uri(id)
        if parsed:
          id = parsed[1]
        try:
          # try numeric comparison first
          greater = int(id) > int(last_activity_id)
        except (TypeError, ValueError):
          greater = id > last_activity_id
        if greater:
          last_activity_id = id

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

      # drop responses without ids
      for resp in replies + likes + reposts + rsvps:
        id = resp.get('id')
        if id:
          resp['activity'] = activity
          responses[id] = resp
        else:
          logging.error('Skipping response without id: %s', resp)

    #
    # Step 3: filter out existing responses
    #
    # existing response ids for each source are cached in memcache (as raw ids,
    # ie *not* tag URIs, to save space). look there first, then fall back to a
    # datastore batch get. it returns full entities, which isn't ideal, so tell
    # it not to cache them to (maybe?) avoid memcache churn.
    #
    # more background: http://stackoverflow.com/questions/11509368
    if responses:
      existing_ids = memcache.get('AR ' + source.bridgy_path())
      if existing_ids is None:
        # batch get from datastore.
        #
        # ideally i'd use a keys only query with an IN filter on key, below, but
        # that results in a separate query per key, and those queries run in
        # serial (!). http://stackoverflow.com/a/11104457/186123
        #
        # existing = Response.query(
        #   Response._key.IN([ndb.Key(Response, id) for id in responses])
        #   ).fetch(len(responses), keys_only=True)
        existing = ndb.get_multi((ndb.Key(Response, id) for id in responses.iterkeys()),
                                 use_memcache=False)
        # (we know Response key ids are always tag URIs)
        existing_ids = [util.parse_tag_uri(e.key.id())[1] for e in existing if e]

      for id in existing_ids:
        responses.pop(source.as_source.tag_uri(id), None)

    #
    # Step 4: store new responses and enqueue propagate tasks
    #
    for id, resp in responses.items():
      activity = resp.pop('activity')
      # we'll usually have multiple responses for the same activity, and the
      # resp['activity'] objects are shared, so cache each activity's discovered
      # webmention targets inside the activity object.
      targets = activity.get('targets')
      if targets is None:
        targets = activity['targets'] = get_webmention_targets(source, activity)
        logging.info('%s has %d original post URL(s): %s', activity.get('url'),
                     len(targets), ' '.join(targets))

      Response(id=id,
               source=source.key,
               activity_json=json.dumps(util.prune_activity(activity)),
               response_json=json.dumps(resp),
               type=Response.get_type(resp),
               unsent=list(targets),
               ).get_or_save()

    if responses:
      # cache newly seen response ids
      memcache.set('AR ' + source.bridgy_path(),
          # (we know Response key ids are always tag URIs)
          existing_ids + [util.parse_tag_uri(id)[1] for id in responses])

    source.last_polled = source.last_poll_attempt
    source.status = 'enabled'
    etag = response.get('etag')
    if last_activity_id and last_activity_id != source.last_activity_id:
      logging.debug('Storing new last activity id: %s', last_activity_id)
      source.last_activity_id = last_activity_id
    if etag and etag != source.last_activities_etag:
      logging.debug('Storing new ETag: %s', etag)
      source.last_activities_etag = etag
    # source is saved in post()


class SendWebmentions(webapp2.RequestHandler):
  """Abstract base task handler that can send webmentions.

  Attributes:
    entity: Webmentions subclass. Set in lease_entity.
  """

  # request deadline (10m) plus some padding
  LEASE_LENGTH = datetime.timedelta(minutes=12)

  ERROR_HTTP_RETURN_CODE = 306  # "Unused"

  def send_webmentions(self, source_url):
    """Tries to send each unsent webmention in self.entity.

    self.lease() *must* be called before this!

    Args:
      source_url: string
    """
    logging.info('Starting %s', self.entity.label())

    try:
      self.do_send_webmentions(source_url)
    except:
      logging.exception('Propagate task failed')
      self.release('error')
      raise

  def do_send_webmentions(self, source_url):
    unsent = set()
    for url in self.entity.unsent + self.entity.error:
      # recheck the url here since the checks may have failed during the poll
      # or streaming add.
      url, domain, ok = util.get_webmention_target(url)
      if ok:
        # When debugging locally, redirect our own webmentions to localhost
        if appengine_config.DEBUG and domain in LOCALHOST_TEST_DOMAINS:
            url = url.replace(domain, 'localhost')
        unsent.add(url)
    self.entity.unsent = sorted(unsent)
    self.entity.error = []

    while self.entity.unsent:
      target = self.entity.unsent.pop(0)
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
          if not mention.send(timeout=999):
            error = mention.error
        except:
          logging.warning('', exc_info=True)
          error = getattr(mention, 'error', None)
          if not error:
            error = {'code': 'EXCEPTION'}

      if error is None:
        logging.info('Sent! %s', mention.response)
        if not self.entity.sent:
          self.set_last_webmention_sent()
        self.entity.sent.append(target)
        memcache.set(cache_key, mention.receiver_endpoint,
                     time=WEBMENTION_DISCOVERY_CACHE_TIME)
      else:
        if error['code'] == 'NO_ENDPOINT':
          logging.info('Giving up this target. %s', error)
          self.entity.skipped.append(target)
          memcache.set(cache_key, error, time=WEBMENTION_DISCOVERY_CACHE_TIME)
        elif (error['code'] == 'BAD_TARGET_URL' and
              error['http_status'] / 100 == 4):
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
  def set_last_webmention_sent(self):
    """Sets this entity's source's last_webmention_sent property to now."""
    source = self.entity.source.get()
    logging.info('Setting last_webmention_sent')
    source.last_webmention_sent = now_fn()
    source.put()


class PropagateResponse(SendWebmentions):
  """Task handler that sends webmentions for a Response.

  Request parameters:
    response_key: string key of Response entity
  """

  def post(self):
    logging.debug('Params: %s', self.request.params)
    if not self.lease(ndb.Key(urlsafe=self.request.params['response_key'])):
      return

    activity = json.loads(self.entity.activity_json)
    response_obj = json.loads(self.entity.response_json)
    if not Source.is_public(response_obj) or not Source.is_public(activity):
      logging.info('Response or activity is non-public. Dropping.')
      self.complete()
      return

    source = self.entity.source.get()
    if not source:
      logging.warning('Source not found! Dropping response.')
      return
    logging.info('Source: %s %s', source.label(), source.key.string_id())

    # (we know Response key ids are always tag URIs)
    _, response_id = util.parse_tag_uri(self.entity.key.string_id())
    if self.entity.type in ('like', 'repost', 'rsvp'):
      response_id = response_id.split('_')[-1]

    # generate local response URL
    parsed = util.parse_tag_uri(activity['id'])
    post_id = parsed[1] if parsed else activity['id']
    # prefer brid-gy.appspot.com to brid.gy because non-browsers (ie OpenSSL)
    # currently have problems with brid.gy's SSL cert. details:
    # https://github.com/snarfed/bridgy/issues/20
    if (self.request.host_url.endswith('brid.gy') or
        self.request.host_url.endswith('brid-gy.appspot.com')):
      host_url = 'https://brid-gy.appspot.com'
    else:
      host_url = self.request.host_url

    local_response_url = '%s/%s/%s/%s/%s/%s' % (
      host_url, self.entity.type, self.entity.source.get().SHORT_NAME,
      self.entity.source.string_id(), post_id, response_id)

    self.send_webmentions(local_response_url)


class PropagateBlogPost(SendWebmentions):
  """Task handler that sends webmentions for a BlogPost.

  Request parameters:
    key: string key of BlogPost entity
  """

  def post(self):
    logging.debug('Params: %s', self.request.params)
    if self.lease(ndb.Key(urlsafe=self.request.params['key'])):
      # skip "self" links to this blog's domain
      source_domain = self.entity.source.get().domain
      to_send = set()
      for url in self.entity.unsent:
        link_domain = util.domain_from_link(url)
        if link_domain and link_domain != source_domain:
          to_send.add(url)
      self.entity.unsent = list(to_send)
      self.send_webmentions(self.entity.key.id())


application = webapp2.WSGIApplication([
    ('/_ah/queue/poll', Poll),
    ('/_ah/queue/propagate', PropagateResponse),
    ('/_ah/queue/propagate-blogpost', PropagateBlogPost),
    ], debug=appengine_config.DEBUG)
