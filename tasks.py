"""Task queue handlers.

TODO: cron job to find sources without seed poll tasks.
TODO: think about how to determine stopping point. can all sources return
comments in strict descending timestamp order? can we require/generate
monotonically increasing comment ids for all sources?
TODO: check HRD consistency guarantees and change as needed
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import copy
import datetime
import json
import logging
import random
import urllib2
import urlparse

import appengine_config

from activitystreams.oauth_dropins.apiclient import errors
from activitystreams.oauth_dropins.python_instagram.bind import InstagramAPIError
from activitystreams.source import Source
# need to import model class definitions since poll creates and saves entities.
import facebook
import googleplus
import instagram
import models
import twitter
import util
from webmentiontools import send

from google.appengine.ext import ndb
import webapp2

# allows injecting timestamps in task_test.py
now_fn = datetime.datetime.now


def get_webmention_targets(activity):
  """Returns a set of string target URLs to attempt to send webmentions to.

  Side effect: runs the original post discovery algorithm on the activity and
  adds the resulting URLs to the activity as tags, in place.
  """
  Source.original_post_discovery(activity)

  targets = set()
  obj = activity.get('object') or activity
  for tag in obj.get('tags', []):
    url = tag.get('url')
    if url and tag.get('objectType') == 'article':
      url, send = util.get_webmention_target(url)
      tag['url'] = url
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
    if not source or source.status == 'disabled':
      logging.error('Source not found or disabled. Dropping task.')
      return
    logging.info('Source: %s %s', source.label(), source.key.string_id())

    last_polled = self.request.params['last_polled']
    if last_polled != source.last_polled.strftime(util.POLL_TASK_DATETIME_FORMAT):
      logging.warning('duplicate poll task! deferring to the other task.')
      return

    source.last_poll_attempt = now_fn()

    try:
      self.do_post(source)
      # randomize task ETA to within +/- 20% of POLL_FREQUENCY to try to spread
      # out tasks and prevent thundering herds.
      countdown = source.POLL_FREQUENCY.seconds * random.uniform(.8, 1.2)
      util.add_poll_task(source, countdown=countdown)
    except models.DisableSource:
      # the user deauthorized the bridgy app, so disable this source.
      # let the task complete successfully so that it's not retried.
      source.status = 'disabled'
      logging.warning('Disabling source!')
    except:
      source.status = 'error'
      raise
    finally:
      source.put()

  def do_post(self, source):
    if source.last_activities_etag or source.last_activity_id:
      logging.debug('Using ETag %s, last activity id %s',
                    source.last_activities_etag, source.last_activity_id)

    try:
      response = source.get_activities_response(
        fetch_replies=True, fetch_likes=True, fetch_shares=True, count=20,
        etag=source.last_activities_etag, min_id=source.last_activity_id)
    except Exception, e:
      if isinstance(e, urllib2.HTTPError):
        code = e.code
      elif isinstance(e, errors.HttpError):
        code = e.resp.status
      elif isinstance(e, InstagramAPIError):
        if e.error_type == 'OAuthAccessTokenException':
          code = '401'
        else:
          code = e.status_code
      else:
        raise

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
        if isinstance(e, urllib2.HTTPError):
          logging.error('Error response body: %r', e.read())
        raise

    activities = response.get('items', [])
    logging.info('Found %d activities', len(activities))
    last_activity_id = source.last_activity_id

    for activity in activities:
      # extract activity id and maybe replace stored last activity id
      id = activity.get('id')
      if id:
        _, id = util.parse_tag_uri(id)
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
      likes = [t for t in tags if models.Response.get_type(t) == 'like']
      reposts = [t for t in tags if models.Response.get_type(t) == 'repost']
      rsvps = Source.get_rsvps_from_event(obj)
      responses = replies + likes + reposts + rsvps

      # drop existing responses
      new_responses = []
      for resp in responses:
        id = resp.get('id')
        if not id:
          logging.error('Skipping response without id: %s', resp)
        elif models.Response.get_by_id(id) is None:
          new_responses.append(resp)

      # short circuit to next activity if none are left to avoid unnecessary
      # extra work resolving original post URLs, etc.
      if not new_responses:
        continue

      targets = get_webmention_targets(activity)
      if targets or new_responses:
        logging.info('%s has %d reply(ies), %d like(s), %d repost(s), and '
                     '%d original post URL(s): %s',
                     activity.get('url'), len(replies), len(likes), len(reposts),
                     len(targets), ' '.join(targets))

      for resp in new_responses:
        models.Response(id=resp['id'],
                        source=source.key,
                        activity_json=json.dumps(activity),
                        response_json=json.dumps(resp),
                        unsent=list(targets),
                        ).get_or_save()

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


class Propagate(webapp2.RequestHandler):
  """Task handler that sends a webmention for a single response.

  Request parameters:
    response_key: string key of response entity
  """

  # request deadline (10m) plus some padding
  LEASE_LENGTH = datetime.timedelta(minutes=12)

  ERROR_HTTP_RETURN_CODE = 306  # "Unused"

  def post(self):
    logging.debug('Params: %s', self.request.params)

    response = self.lease_response()
    if not response:
      return

    activity = json.loads(response.activity_json)
    response_obj = json.loads(response.response_json)
    if not Source.is_public(response_obj) or not Source.is_public(activity):
      logging.info('Response or activity is non-public. Dropping.')
      self.complete_response(response)
      return

    source = response.source.get()
    if not source:
      logging.warning('Source not found! Dropping response.')
      return
    logging.info('Source: %s %s', source.label(), source.key.string_id())

    try:
      logging.info('Starting %s response %s',
                   response.source.kind(), response.key.string_id())

      _, response_id = util.parse_tag_uri(response.key.string_id())
      if response.type in ('like', 'repost', 'rsvp'):
        response_id = response_id.split('_')[-1]

      # generate local response URL
      _, post_id = util.parse_tag_uri(activity['id'])
      # prefer brid-gy.appspot.com to brid.gy because non-browsers (ie OpenSSL)
      # currently have problems with brid.gy's SSL cert. details:
      # https://github.com/snarfed/bridgy/issues/20
      if (self.request.host_url.endswith('brid.gy') or
          self.request.host_url.endswith('brid-gy.appspot.com')):
        host_url = 'https://brid-gy.appspot.com'
      else:
        host_url = self.request.host_url

      local_response_url = '%s/%s/%s/%s/%s/%s' % (
        host_url, response.type, response.source.get().SHORT_NAME,
        response.source.string_id(), post_id, response_id)

      # send each webmention. recheck the url here since the checks may have failed
      # during the poll or streaming add.
      unsent = set()
      for url in response.unsent + response.error:
        url, ok = util.get_webmention_target(url)
        if ok:
          unsent.add(url)
      response.unsent = sorted(unsent)
      response.error = []

      while response.unsent:
        target = response.unsent.pop(0)

        # When debugging locally, redirect my (snarfed.org) webmentions to localhost
        if appengine_config.DEBUG and target.startswith('http://snarfed.org/'):
          target = target.replace('http://snarfed.org/', 'http://localhost/')

        # send! and handle response or error
        mention = send.WebmentionSend(local_response_url, target)
        logging.info('Sending webmention from %s to %s', local_response_url, target)
        sent = False
        try:
          sent = mention.send(timeout=999)
        except:
          logging.warning('', exc_info=True)
          if not getattr(mention, 'error', None):
            mention.error = {'code': 'EXCEPTION'}

        if sent:
          logging.info('Sent! %s', mention.response)
          response.sent.append(target)
        else:
          if mention.error['code'] == 'NO_ENDPOINT':
            logging.info('Giving up this target. %s', mention.error)
            response.skipped.append(target)
          elif (mention.error['code'] == 'BAD_TARGET_URL' and
                mention.error['http_status'] / 100 == 4):
            # Give up on 4XX errors; we don't expect later retries to succeed.
            logging.info('Giving up this target. %s', mention.error)
            response.failed.append(target)
          else:
            self.fail('Error sending to endpoint: %s' % mention.error)
            response.error.append(target)

        if target in response.unsent:
          response.unsent.remove(target)

      if response.error:
        logging.warning('Propagate task failed')
        self.release_response(response, 'error')
      else:
        self.complete_response(response)

    except:
      logging.exception('Propagate task failed')
      self.release_response(response, 'error')
      raise

  @ndb.transactional
  def lease_response(self):
    """Attempts to acquire and lease the response entity.

    Returns the Response on success, otherwise None.

    TODO: unify with complete_response
    """
    response = ndb.Key(urlsafe=self.request.params['response_key']).get()

    if response is None:
      self.fail('no response entity!')
    elif response.status == 'complete':
      # let this response return 200 and finish
      logging.warning('duplicate task already propagated response')
    elif response.status == 'processing' and now_fn() < response.leased_until:
      self.fail('duplicate task is currently processing!')
    else:
      assert response.status in ('new', 'processing', 'error')
      response.status = 'processing'
      response.leased_until = now_fn() + self.LEASE_LENGTH
      response.put()
      return response

  @ndb.transactional
  def complete_response(self, response):
    """Attempts to mark the response entity completed.

    Returns True on success, False otherwise.

    Args:
      response: models.Response
    """
    existing = response.key.get()
    if existing is None:
      self.fail('response entity disappeared!', level=logging.ERROR)
    elif existing.status == 'complete':
      # let this response return 200 and finish
      logging.warning('response stolen and finished. did my lease expire?')
      return False
    elif existing.status == 'new':
      self.fail('response went backward from processing to new!',
                level=logging.ERROR)

    assert response.status == 'processing'
    response.status = 'complete'
    response.put()
    return True

  @ndb.transactional
  def release_response(self, response, new_status):
    """Attempts to unlease the response entity.

    Args:
      response: models.Response
      new_status: string
    """
    existing = response.key.get()
    if existing and existing.status == 'processing':
      response.status = new_status
      response.leased_until = None
      response.put()

  def fail(self, message, level=logging.WARNING):
    """Fills in an error response status code and message.
    """
    self.error(self.ERROR_HTTP_RETURN_CODE)
    logging.log(level, message)
    self.response.out.write(message)


application = webapp2.WSGIApplication([
    ('/_ah/queue/poll', Poll),
    ('/_ah/queue/propagate', Propagate),
    ], debug=appengine_config.DEBUG)
