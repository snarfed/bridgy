"""Unit tests for models.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import urllib

from models import Comment, Source
import testutil
from testutil import FakeSource
import util

from activitystreams import source as as_source
from google.appengine.api import users
from google.appengine.ext import testbed


class CommentTest(testutil.ModelsTest):

  def test_get_or_save(self):
    self.sources[0].save()

    comment = self.comments[0]
    self.assertEqual(0, Comment.all().count())
    self.assertEqual(0, len(self.taskqueue_stub.GetTasks('propagate')))

    # new. should add a propagate task.
    saved = comment.get_or_save()
    self.assertTrue(saved.is_saved())
    self.assertEqual(comment.key(), saved.key())
    self.assertEqual(comment.source, saved.source)

    tasks = self.taskqueue_stub.GetTasks('propagate')
    self.assertEqual(1, len(tasks))
    self.assertEqual(str(comment.key()),
                     testutil.get_task_params(tasks[0])['comment_key'])
    self.assertEqual('/_ah/queue/propagate', tasks[0]['url'])

    # existing. no new task.
    same = saved.get_or_save()
    self.assertEqual(saved.source.key(), same.source.key())
    self.assertEqual(1, len(tasks))


class SourceTest(testutil.HandlerTest):

  def _test_create_new(self):
    FakeSource.create_new(self.handler)
    self.assertEqual(1, FakeSource.all().count())

    tasks = self.taskqueue_stub.GetTasks('poll')
    self.assertEqual(1, len(tasks))
    source = FakeSource.all().get()
    self.assertEqual('/_ah/queue/poll', tasks[0]['url'])
    params = testutil.get_task_params(tasks[0])
    self.assertEqual(str(source.key()), params['source_key'])
    self.assertEqual('1970-01-01-00-00-00',
                     params['last_polled'])

  def test_create_new(self):
    self.assertEqual(0, FakeSource.all().count())
    self._test_create_new()
    self.assertEqual([urllib.quote_plus('Added FakeSource: fake')],
                     self.handler.messages)

  def test_create_new_already_exists(self):
    FakeSource.new(None).save()
    FakeSource.key_name_counter -= 1
    self._test_create_new()
    self.assertEqual([urllib.quote_plus('Updated existing FakeSource: fake')],
                     self.handler.messages)

  def test_get_post(self):
    post = {'verb': 'post', 'object': {'objectType': 'note', 'content': 'asdf'}}
    source = Source(key_name='x')
    self.mox.StubOutWithMock(source, 'get_activities')
    source.get_activities(activity_id='123').AndReturn(([post]))

    self.mox.ReplayAll()
    self.assert_equals(post, source.get_post('123'))

  def test_get_comment(self):
    comment_obj = {'objectType': 'comment', 'content': 'qwert'}
    source = FakeSource.new(None)
    source.as_source = self.mox.CreateMock(as_source.Source)
    source.as_source.get_comment('123', activity_id=None).AndReturn(comment_obj)

    self.mox.ReplayAll()
    self.assert_equals(comment_obj, source.get_comment('123'))
