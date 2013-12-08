"""Unit tests for tasks.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import json
import logging
import mox
import urllib
import urlparse

import models
import models_test
import tasks
from tasks import Poll, Propagate
import testutil
import util
from webmentiontools import send

from google.appengine.ext import db
import webapp2

NOW = datetime.datetime.now()
tasks.now_fn = lambda: NOW


class TaskQueueTest(testutil.ModelsTest):
  """Attributes:
    task_params: the query parameters passed in the task POST request
    post_url: the URL for post_task() to post to
  """
  task_params = None
  post_url = None

  def post_task(self, expected_status=200):
    """Args:
      expected_status: integer, the expected HTTP return code
    """
    resp = tasks.application.get_response(self.post_url, method='POST',
                                          body=urllib.urlencode(self.task_params))
    self.assertEqual(expected_status, resp.status_int)


class PollTest(TaskQueueTest):

  post_url = '/_ah/queue/poll'

  def setUp(self):
    super(PollTest, self).setUp()
    self.task_params = {'source_key': self.sources[0].key(),
                        'last_polled': '1970-01-01-00-00-00'}

  def assert_comments(self):
    """Asserts that all of self.comments are saved."""
    self.assert_entities_equal(self.comments, models.Comment.all())

  def test_poll(self):
    """A normal poll task."""
    self.assertEqual([], list(models.Comment.all()))
    self.assertEqual([], self.taskqueue_stub.GetTasks('poll'))

    self.post_task()
    self.assert_comments()

    source = db.get(self.sources[0].key())
    self.assertEqual(NOW, source.last_polled)

    tasks = self.taskqueue_stub.GetTasks('poll')
    self.assertEqual(1, len(tasks))
    self.assertEqual('/_ah/queue/poll', tasks[0]['url'])

    params = testutil.get_task_params(tasks[0])
    self.assertEqual(str(source.key()),
                     params['source_key'])
    self.assertEqual(NOW.strftime(util.POLL_TASK_DATETIME_FORMAT),
                     params['last_polled'])

  def test_existing_comments(self):
    """Poll should be idempotent and not touch existing comment entities.
    """
    self.comments[0].status = 'complete'
    self.comments[0].save()

    self.post_task()
    self.assert_comments()
    self.assertEqual('complete', db.get(self.comments[0].key()).status)

  def test_wrong_last_polled(self):
    """If the source doesn't have our last polled value, we should quit.
    """
    self.sources[0].last_polled = datetime.datetime.utcfromtimestamp(3)
    self.sources[0].save()
    self.post_task()
    self.assertEqual([], list(models.Comment.all()))

  def test_no_source(self):
    """If the source doesn't exist, do nothing and let the task die.
    """
    self.sources[0].delete()
    self.post_task()
    self.assertEqual([], self.taskqueue_stub.GetTasks('poll'))

  def test_disable_source_on_deauthorized(self):
    """If the source raises DisableSource, disable it.
    """
    source = self.sources[0]
    self.mox.StubOutWithMock(testutil.FakeSource, 'get_activities')
    testutil.FakeSource.get_activities(count=mox.IgnoreArg(), fetch_replies=True
                                       ).AndRaise(models.DisableSource)
    self.mox.ReplayAll()

    source.status = 'enabled'
    source.save()
    self.post_task()
    source = db.get(source.key())
    self.assertEqual('disabled', source.status)


class PropagateTest(TaskQueueTest):

  post_url = '/_ah/queue/propagate'

  def setUp(self):
    super(PropagateTest, self).setUp()
    self.comments[0].save()
    self.task_params = {'comment_key': self.comments[0].key()}

  def assert_comment_is(self, status, leased_until=False):
    """Asserts that comments[0] has the given values in the datastore.
    """
    comment = db.get(self.comments[0].key())
    self.assertEqual(status, comment.status)
    if leased_until is not False:
      self.assertEqual(leased_until, comment.leased_until)

  def mock_webmention(self):
    self.mock_send = self.mox.CreateMock(send.WebmentionSend)
    self.mock_send.receiver_endpoint = 'http://webmention/endpoint'
    self.mock_send.response = 'used in logging'
    self.mox.StubOutWithMock(send, 'WebmentionSend', use_mock_anything=True)

  def expect_webmention(self, target_url='http://target1/post/url'):
    self.mock_webmention()
    local_url = 'http://localhost/comment/fake/%s/000/1_2_a' % \
      self.comments[0].source.key().name()
    send.WebmentionSend(local_url, target_url).AndReturn(self.mock_send)
    return self.mock_send.send(timeout=999)

  def test_propagate(self):
    """A normal propagate task."""
    self.assertEqual('new', self.comments[0].status)

    self.expect_webmention().AndReturn(True)
    self.mox.ReplayAll()
    self.post_task()
    self.assert_comment_is('complete', NOW + Propagate.LEASE_LENGTH)

  def test_propagate_from_error(self):
    """A normal propagate task, with a comment starting as 'error'."""
    self.comments[0].status = 'error'
    self.comments[0].save()

    self.expect_webmention().AndReturn(True)
    self.mox.ReplayAll()
    self.post_task()
    self.assert_comment_is('complete', NOW + Propagate.LEASE_LENGTH)

  def test_original_post_discovery(self):
    """Target URLs should be extracted from attachments, tags, and text."""
    activity = json.loads(self.comments[0].activity_json)
    obj = activity['object']
    obj['tags'] = [{'objectType': 'article', 'url': 'http://tar.get/a'},
                   {'objectType': 'person', 'url': 'http://pe.rs/on'},
                   ]
    obj['attachments'] = [{'objectType': 'article', 'url': 'http://tar.get/b'}]
    obj['content'] = 'foo http://tar.get/c bar (tar.get d) baz'
    self.comments[0].activity_json = json.dumps(activity)
    self.comments[0].save()

    source_name = self.comments[0].source.key().name()
    local_url = 'http://localhost/comment/fake/%s/000/1_2_a' % source_name
    self.mock_webmention()
    for i in 'a', 'b', 'c', 'd':
      target = 'http://tar.get/%s' % i
      send.WebmentionSend(local_url, target).InAnyOrder().AndReturn(self.mock_send)
      self.mock_send.send(timeout=999).InAnyOrder().AndReturn(True)

    self.mox.ReplayAll()
    self.post_task()

  def test_already_complete(self):
    """If the comment has already been propagated, do nothing."""
    self.comments[0].status = 'complete'
    self.comments[0].save()

    self.post_task()
    self.assert_comment_is('complete')

  def test_leased(self):
    """If the comment is processing and the lease hasn't expired, do nothing."""
    self.comments[0].status = 'processing'
    leased_until = NOW + datetime.timedelta(minutes=1)
    self.comments[0].leased_until = leased_until
    self.comments[0].save()

    self.post_task(expected_status=Propagate.ERROR_HTTP_RETURN_CODE)
    self.assert_comment_is('processing', leased_until)

    comment = db.get(self.comments[0].key())
    self.assertEqual('processing', comment.status)
    self.assertEqual(leased_until, comment.leased_until)

  def test_lease_expired(self):
    """If the comment is processing but the lease has expired, process it."""
    self.comments[0].status = 'processing'
    self.comments[0].leased_until = NOW - datetime.timedelta(minutes=1)
    self.comments[0].save()

    self.expect_webmention().AndReturn(True)
    self.mox.ReplayAll()
    self.post_task()
    self.assert_comment_is('complete', NOW + Propagate.LEASE_LENGTH)

  def test_no_comment(self):
    """If the comment doesn't exist, the request should fail."""
    self.comments[0].delete()
    self.post_task(expected_status=Propagate.ERROR_HTTP_RETURN_CODE)

  def test_webmention_fail(self):
    """If sending the webmention fails, the lease should be released."""
    for code, give_up in (('NO_ENDPOINT', True),
                          ('BAD_TARGET_URL', False),
                          ('RECEIVER_ERROR', False)):
      self.mox.UnsetStubs()
      self.comments[0].status = 'new'
      self.comments[0].save()
      self.expect_webmention().AndReturn(False)
      self.mock_send.error = {'code': code}
      self.mox.ReplayAll()

      logging.debug('Testing %s', code)
      expected_status = 200 if give_up else Propagate.ERROR_HTTP_RETURN_CODE
      self.post_task(expected_status=expected_status)
      self.assert_comment_is('complete' if give_up else 'error')
      self.mox.VerifyAll()

  def test_webmention_exception(self):
    """If sending the webmention raises an exception, the lease should be released."""
    self.expect_webmention().AndRaise(Exception('foo'))
    self.mox.ReplayAll()

    self.post_task(expected_status=500)
    self.assert_comment_is('error', None)

  def test_lease_exception(self):
    """If leasing raises an exception, the lease should be released."""
    self.mox.StubOutWithMock(Propagate, 'lease_comment')
    Propagate.lease_comment().AndRaise(Exception('foo'))
    self.mox.ReplayAll()

    self.post_task(expected_status=500)
    self.assert_comment_is('new', None)

  def test_complete_exception(self):
    """If completing raises an exception, the lease should be released."""
    self.expect_webmention().AndReturn(True)
    self.mox.StubOutWithMock(Propagate, 'complete_comment')
    Propagate.complete_comment().AndRaise(Exception('foo'))
    self.mox.ReplayAll()

    self.post_task(expected_status=500)
    self.assert_comment_is('error', None)
