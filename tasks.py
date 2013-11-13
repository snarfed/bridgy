"""Task queue handlers.

TODO: cron job to find sources without seed poll tasks.
TODO: think about how to determine stopping point. can all sources return
comments in strict descending timestamp order? can we require/generate
monotonically increasing comment ids for all sources?
TODO: check HRD consistency guarantees and change as needed
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import itertools
import logging
import re
import time

# need to import model class definitions since poll creates and saves entities.
import facebook
import googleplus
import instagram
import models
import twitter
import util

from google.appengine.ext import db
from google.appengine.api import taskqueue
import webapp2

import appengine_config

# all concrete destination model classes
DESTINATIONS = []

# allows injecting timestamps in task_test.py
now_fn = datetime.datetime.now


class Poll(webapp2.RequestHandler):
  """Task handler that fetches and processes new comments from a single source.

  Request parameters:
    source_key: string key of source entity
    last_polled: timestamp, YYYY-MM-DD-HH-MM-SS

  Inserts a propagate task for each comment that hasn't been seen before.
  """

  TASK_COUNTDOWN = datetime.timedelta(hours=1)

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
    # itertools.chain flattens. also, the outer list() is important, because
    # itertools.chain returns a generator, and we need to be able to iterate
    # over it multiple times. TODO: unit test this
    dests = list(itertools.chain(*[list(db.GqlQuery('SELECT * FROM %s' % cls))
                                   for cls in DESTINATIONS]))

    logging.debug('Polling %s source %s against destinations %r',
                  source.kind(), source.key().name(), [d.url for d in dests])

    if dests:
      posts_and_dests = []

      for post, url in source.get_posts():
        # can't use this string prefix query code because we want the property
        # that's a prefix of the filter value, not vice versa.
        # query = db.GqlQuery(
        #   'SELECT * FROM WordPressSite WHERE url = :1 AND url <= :2',
        #   url, url + u'\ufffd')
        dest = [d for d in dests if url.startswith(d.url)]
        assert len(dest) <= 1
        if dest:
          dest = dest[0]
          posts_and_dests.append((post, dest))

      for comment in source.get_comments(posts_and_dests):
        comment.get_or_save()

    source.last_polled = now_fn()
    util.add_poll_task(source, countdown=self.TASK_COUNTDOWN.seconds)
    source.save()


class Propagate(webapp2.RequestHandler):
  """Task handler that propagates a single comment.

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
      if comment:
        comment.dest.add_comment(comment)
        self.complete_comment()
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
