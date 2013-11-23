"""Task queue handlers.

TODO: cron job to find sources without seed poll tasks.
TODO: think about how to determine stopping point. can all sources return
comments in strict descending timestamp order? can we require/generate
monotonically increasing comment ids for all sources?
TODO: check HRD consistency guarantees and change as needed
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import json
import logging

# need to import model class definitions since poll creates and saves entities.
import facebook
import googleplus
import instagram
import models
import twitter
import util
from webmentiontools import send

from google.appengine.ext import db
from google.appengine.api import taskqueue
import webapp2

import appengine_config

# allows injecting timestamps in task_test.py
now_fn = datetime.datetime.now


class Poll(webapp2.RequestHandler):
  """Task handler that fetches and processes new comments from a single source.

  Request parameters:
    source_key: string key of source entity
    last_polled: timestamp, YYYY-MM-DD-HH-MM-SS

  Inserts a propagate task for each comment that hasn't been seen before.
  """

  TASK_COUNTDOWN = datetime.timedelta(minutes=15)

  def post(self):
    logging.debug('Params: %s', self.request.params)

    key = self.request.params['source_key']
    source = db.get(key)
    if not source:
      logging.warning('Source not found! Dropping task.')
      return

    last_polled = self.request.params['last_polled']
    if last_polled != source.last_polled.strftime(util.POLL_TASK_DATETIME_FORMAT):
      logging.warning('duplicate poll task! deferring to the other task.')
      return

    try:
      self.do_post(source)
    except models.DisableSource:
      # the user deauthorized the bridgy app, so disable this source.
      source.status = 'disabled'
      source.save()
      logging.error('Disabling source!')
      # let this task complete successfully so that it's not retried.

  def do_post(self, source):
    logging.info('Polling %s %s %s', source.type_display_name(),
                 source.key().name(), source.name)
    activities = source.get_activities(count=5)
    logging.info('Found %d activities', len(activities))

    for activity in activities:
      # remove replies from activity JSON so we don't store them all in every
      # Comment entity.
      replies = activity['object'].pop('replies', {}).get('items', [])
      logging.info('Found %d comments for activity %s', len(replies),
                   activity.get('url'))
      for reply in replies:
        models.Comment(key_name=reply['id'],
                       source=source,
                       activity_json=json.dumps(activity),
                       comment_json=json.dumps(reply),
                       ).get_or_save()

    source.last_polled = now_fn()
    util.add_poll_task(source, countdown=self.TASK_COUNTDOWN.seconds)
    source.save()


class Propagate(webapp2.RequestHandler):
  """Task handler that sends a webmention for a single comment.

  Request parameters:
    comment_key: string key of comment entity
  """

  # request deadline (10m) plus some padding
  LEASE_LENGTH = datetime.timedelta(minutes=12)

  ERROR_HTTP_RETURN_CODE = 417  # Expectation Failed

  def post(self):
    logging.debug('Params: %s', self.request.params)

    try:
      comment = self.lease_comment()
      if not comment:
        return

      local_comment_url = self.request.host_url + comment.local_path()
      logging.info('Starting %s comment %s',
                   comment.source.kind(), comment.key().name())

      activity = json.loads(comment.activity_json)
      comment.source.as_source.original_post_discovery(activity)
      targets = util.trim_nulls(
        t.get('url') for t in activity['object'].get('tags', []))

      # send webmentions!
      logging.info('Discovered original post URLs in %s: %s',
                   comment.key().name(), targets)
      for target in targets:
        # When debugging locally, redirect my (snarfed.org) webmentions to localhost
        if appengine_config.DEBUG and target.startswith('http://snarfed.org/'):
          target = target.replace('http://snarfed.org/', 'http://localhost/')

        mention = send.WebmentionSend(local_comment_url, target)
        logging.info('Sending webmention from %s to %s', local_comment_url, target)
        if mention.send():
          logging.info('Sent to %s', mention.receiver_endpoint)
          self.complete_comment()
        else:
          if mention.error['code'] == 'NO_ENDPOINT':
            logging.info('No webmention endpoint found, giving up this comment.')
            self.complete_comment()
          else:
            self.fail('Error sending to endpoint %s: %s' %
                      (mention.receiver_endpoint, mention.error))
            self.release_comment()

    except:
      logging.exception('Propagate task failed')
      self.release_comment()
      raise

  @db.transactional
  def lease_comment(self):
    """Attempts to acquire and lease the comment entity.

    Returns the Comment on success, otherwise None.

    TODO: unify with complete_comment
    """
    comment = db.get(self.request.params['comment_key'])

    if comment is None:
      self.fail('no comment entity!')
    elif comment.status == 'complete':
      # let this response return 200 and finish
      logging.warning('duplicate task already propagated comment')
    elif comment.status == 'processing' and now_fn() < comment.leased_until:
      self.fail('duplicate task is currently processing!')
    else:
      assert comment.status in ('new', 'processing')
      comment.status = 'processing'
      comment.leased_until = now_fn() + self.LEASE_LENGTH
      comment.save()
      return comment

  @db.transactional
  def complete_comment(self):
    """Attempts to mark the comment entity completed.

    Returns True on success, False otherwise.
    """
    comment = db.get(self.request.params['comment_key'])

    if comment is None:
      self.fail('comment entity disappeared!')
    elif comment.status == 'complete':
      # let this response return 200 and finish
      logging.warning('comment stolen and finished. did my lease expire?')
    elif comment.status == 'new':
      self.fail('comment went backward from processing to new!')
    else:
      assert comment.status == 'processing'
      comment.status = 'complete'
      comment.save()
      return True

    return False

  @db.transactional
  def release_comment(self):
    """Attempts to unlease the comment entity.
    """
    comment = db.get(self.request.params['comment_key'])
    if comment and comment.status == 'processing':
      comment.status = 'new'
      comment.leased_until = None
      comment.save()

  def fail(self, message):
    """Fills in an error response status code and message.
    """
    self.error(self.ERROR_HTTP_RETURN_CODE)
    logging.error(message)
    self.response.out.write(message)


application = webapp2.WSGIApplication([
    ('/_ah/queue/poll', Poll),
    ('/_ah/queue/propagate', Propagate),
    ], debug=appengine_config.DEBUG)
