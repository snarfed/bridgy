"""Unit tests for models.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import json
import urllib

from models import Response, Source
import testutil
from testutil import FakeSource
import util

from activitystreams import source as as_source
from google.appengine.api import users
from google.appengine.ext import testbed


class ResponseTest(testutil.ModelsTest):

  def test_get_or_save(self):
    self.sources[0].put()

    response = self.responses[0]
    self.assertEqual(0, Response.query().count())
    self.assertEqual(0, len(self.taskqueue_stub.GetTasks('propagate')))

    # new. should add a propagate task.
    saved = response.get_or_save()
    self.assertEqual(response.key, saved.key)
    self.assertEqual(response.source, saved.source)
    self.assertEqual('comment', saved.type)

    tasks = self.taskqueue_stub.GetTasks('propagate')
    self.assertEqual(1, len(tasks))
    self.assertEqual(response.key.urlsafe(),
                     testutil.get_task_params(tasks[0])['response_key'])
    self.assertEqual('/_ah/queue/propagate', tasks[0]['url'])

    # existing. no new task.
    same = saved.get_or_save()
    self.assertEqual(saved.source, same.source)
    self.assertEqual(1, len(tasks))

  def test_get_or_save_objectType_note(self):
    self.responses[0].response_json = json.dumps({
      'objectType': 'note',
      'id': 'tag:source.com,2013:1_2_%s' % id,
      })
    saved = self.responses[0].get_or_save()
    self.assertEqual('comment', saved.type)

  def test_dom_id(self):
    self.assertEqual('fake-%s' % self.sources[0].key.string_id(),
                     self.sources[0].dom_id())

  def test_get_type(self):
    self.assertEqual('repost', Response.get_type(
        {'objectType': 'activity', 'verb': 'share'}))
    self.assertEqual('rsvp', Response.get_type({'verb': 'rsvp-no'}))
    self.assertEqual('rsvp', Response.get_type({'verb': 'invite'}))
    self.assertEqual('comment', Response.get_type({'objectType': 'other'}))


class SourceTest(testutil.HandlerTest):

  def _test_create_new(self):
    FakeSource.create_new(self.handler)
    self.assertEqual(1, FakeSource.query().count())

    tasks = self.taskqueue_stub.GetTasks('poll')
    self.assertEqual(1, len(tasks))
    source = FakeSource.query().get()
    self.assertEqual('/_ah/queue/poll', tasks[0]['url'])
    params = testutil.get_task_params(tasks[0])
    self.assertEqual(source.key.urlsafe(), params['source_key'])
    self.assertEqual('1970-01-01-00-00-00',
                     params['last_polled'])

  def test_create_new(self):
    self.assertEqual(0, FakeSource.query().count())
    self._test_create_new()
    msg = "Added fake (FakeSource). Refresh to see what we've found!"
    self.assert_equals({msg}, self.handler.messages)

  def test_create_new_already_exists(self):
    FakeSource.new(None).put()
    FakeSource.string_id_counter -= 1
    self._test_create_new()
    msg = "Updated fake (FakeSource). Refresh to see what's new!"
    self.assert_equals({msg}, self.handler.messages)

  def test_get_post(self):
    post = {'verb': 'post', 'object': {'objectType': 'note', 'content': 'asdf'}}
    source = Source(id='x')
    self.mox.StubOutWithMock(source, 'get_activities')
    source.get_activities(activity_id='123', user_id='x').AndReturn([post])

    self.mox.ReplayAll()
    self.assert_equals(post, source.get_post('123'))

  def test_get_comment(self):
    comment_obj = {'objectType': 'comment', 'content': 'qwert'}
    source = FakeSource.new(None)
    source.as_source = self.mox.CreateMock(as_source.Source)
    source.as_source.get_comment('123', activity_id=None).AndReturn(comment_obj)

    self.mox.ReplayAll()
    self.assert_equals(comment_obj, source.get_comment('123'))
