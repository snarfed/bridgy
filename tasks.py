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
import urllib2
import urlparse

from activitystreams.source import Source
import appengine_config
# need to import model class definitions since poll creates and saves entities.
import facebook
import googleplus
import instagram
import models
from oauth_dropins.apiclient import errors
from oauth_dropins.python_instagram.bind import InstagramAPIError
import twitter
import util
from webmentiontools import send

from google.appengine.ext import db
from google.appengine.api import taskqueue
import webapp2

import appengine_config

# allows injecting timestamps in task_test.py
now_fn = datetime.datetime.now


def get_webmention_targets(activity):
  """Returns a set of string target URLs to attempt to send webmentions to.

  Side effect: runs the original post discovery algorithm on the activity and
  adds the resulting URLs to the activity as tags, in place.
  """
  Source.original_post_discovery(activity)

  targets = set()
  for tag in activity['object'].get('tags', []):
    url = tag.get('url')

    if url and tag.get('objectType') == 'article':
      try:
        urlparse.urlparse(url)
      except Exception, e:
        logging.warning('Dropping bad URL %s.', url)
        continue
      if util.in_webmention_blacklist(url):
        continue
      resolved = util.follow_redirects(url)
      if resolved != url:
        logging.debug('Resolved %s to %s', url, resolved)
        tag['url'] = resolved
      if util.in_webmention_blacklist(resolved):
        continue
      targets.add(resolved)

  return targets


class Poll(webapp2.RequestHandler):
  """Task handler that fetches and processes new responses from a single source.

  Request parameters:
    source_key: string key of source entity
    last_polled: timestamp, YYYY-MM-DD-HH-MM-SS

  Inserts a propagate task for each response that hasn't been seen before.
  """

  TASK_COUNTDOWN = datetime.timedelta(minutes=5)

  def post(self):
    logging.debug('Params: %s', self.request.params)

    key = self.request.params['source_key']
    source = db.get(key)
    if not source:
      logging.warning('Source not found! Dropping task.')
      return
    logging.info('Source: %s %s', source.label(), source.key().name())

    last_polled = self.request.params['last_polled']
    if last_polled != source.last_polled.strftime(util.POLL_TASK_DATETIME_FORMAT):
      logging.warning('duplicate poll task! deferring to the other task.')
      return

    source.last_poll_attempt = now_fn()

    try:
      self.do_post(source)
      util.add_poll_task(source, countdown=self.TASK_COUNTDOWN.seconds)
    except models.DisableSource:
      # the user deauthorized the bridgy app, so disable this source.
      # let the task complete successfully so that it's not retried.
      source.status = 'disabled'
      logging.warning('Disabling source!')
    except:
      source.status = 'error'
      raise
    finally:
      source.save()

  def do_post(self, source):
    if source.last_activities_etag or source.last_activity_id:
      logging.debug('Using ETag %s, last activity id %s',
                    source.last_activities_etag, source.last_activity_id)

    try:
      response = source.get_activities_response(
        fetch_replies=True, fetch_likes=True, fetch_shares=True, count=20,
        etag=source.last_activities_etag, min_id=source.last_activity_id)
    except (urllib2.HTTPError, errors.HttpError, InstagramAPIError), e:
      if isinstance(e, urllib2.HTTPError):
        code = e.code
      elif isinstance(e, errors.HttpError):
        code = e.resp.status
      else:
        assert isinstance(e, InstagramAPIError), e
        code = e.status_code

      code = str(code)
      if code == '401':
        msg = 'Unauthorized error: %s' % e
        logging.exception(msg)
        raise models.DisableSource(msg)
      elif code in ('403', '429', '503'):
        # rate limiting errors. twitter returns 429, instagram 503, google+ 403.
        # TODO: facebook. it returns 200 and reports the error in the response.
        # https://developers.facebook.com/docs/reference/ads-api/api-rate-limiting/
        logging.error('Rate limited. Marking as error and finishing task. %s', e)
        source.status = 'error'
        return
      else:
        raise

    activities = response.get('items', [])
    logging.info('Found %d new activities', len(activities))
    last_activity_id = source.last_activity_id

    for activity in activities:
      targets = get_webmention_targets(activity)

      # extract replies, likes, and reposts.
      obj = activity['object']
      replies = obj.get('replies', {}).get('items', [])
      tags = obj.get('tags', [])
      likes = [t for t in tags if models.Response.get_type(t) == 'like']
      reposts = [t for t in tags if models.Response.get_type(t) == 'repost']

      responses = replies + likes + reposts
      if targets or responses:
        logging.info('%s has %d reply(ies), %d like(s), %d repost(s), and '
                     '%d original post URL(s): %s',
                     activity.get('url'), len(replies), len(likes), len(reposts),
                     len(targets), ' '.join(targets))

      for resp in responses:
        id = resp.get('id')
        if not id:
          logging.error('Skipping response without id: %s', resp)
          continue
        models.Response(key_name=id,
                        source=source,
                        activity_json=json.dumps(activity),
                        response_json=json.dumps(resp),
                        unsent=list(targets),
                        ).get_or_save()

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

    try:
      source = response.source
    except db.ReferencePropertyResolveError:
      logging.warning('Source not found! Dropping task.')
      return
    logging.info('Source: %s %s', source.label(), source.key().name())

    try:
      logging.info('Starting %s response %s',
                   response.source.kind(), response.key().name())

      _, response_id = util.parse_tag_uri(response.key().name())
      if response.type in ('like', 'repost'):
        response_id = response_id.split('_')[-1]

      # generate local response URL
      activity = json.loads(response.activity_json)
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
        host_url, response.type, response.source.SHORT_NAME,
        response.source.key().name(), post_id, response_id)

      # send each webmention. recheck the blacklist here so that we can add to
      # it and have the additions apply to existing propagate tasks.
      response.unsent = sorted(set(url for url in response.unsent + response.error
                                   if not util.in_webmention_blacklist(url)))
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
          logging.exception('')
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

  @db.transactional
  def lease_response(self):
    """Attempts to acquire and lease the response entity.

    Returns the Response on success, otherwise None.

    TODO: unify with complete_response
    """
    response = db.get(self.request.params['response_key'])

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
      response.save()
      return response

  @db.transactional
  def complete_response(self, response):
    """Attempts to mark the response entity completed.

    Returns True on success, False otherwise.
    """
    existing = db.get(response.key())
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
    response.save()
    return True

  @db.transactional
  def release_response(self, response, new_status):
    """Attempts to unlease the response entity.
    """
    existing = db.get(response.key())
    if existing and existing.status == 'processing':
      response.status = new_status
      response.leased_until = None
      response.save()

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
