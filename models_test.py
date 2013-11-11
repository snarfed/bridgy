#!/usr/bin/python
"""Unit tests for models.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

from models import Comment, Site, User
import testutil
from testutil import FakeSite, FakeSource, FakeDestination
import util

from google.appengine.api import users


class CommentTest(testutil.ModelsTest):

  def test_get_or_save(self):
    self.sources[0].save()

    comment = self.comments[0]
    self.assertEqual(0, Comment.all().count())
    self.assertEqual(0, len(self.taskqueue_stub.GetTasks('propagate')))

    # new. should add a propagate task.
    saved = comment.get_or_save()
    self.assertEqual(1, Comment.all().count())
    self.assertTrue(saved.is_saved())
    self.assertEqual(comment.key(), saved.key())
    self.assertEqual(comment.source, saved.source)
    self.assertEqual(comment.dest, saved.dest)

    tasks = self.taskqueue_stub.GetTasks('propagate')
    self.assertEqual(1, len(tasks))
    self.assertEqual(str(comment.key()),
                     testutil.get_task_params(tasks[0])['comment_key'])
    self.assertEqual('/_ah/queue/propagate', tasks[0]['url'])

    # existing. no new task.
    same = saved.get_or_save()
    self.assertEqual(saved.source.key(), same.source.key())
    self.assertEqual(saved.dest.key(), same.dest.key())
    self.assertEqual(1, len(tasks))

    # # different source and dest
    # # i don't do this assert any more, but i might come back to it later.
    # diff = Comment(key_name=comment.key().name(),
    #                source=self.sources[0], dest=self.dests[1])
    # self.assertRaises(AssertionError, diff.get_or_save)
    # diff = Comment(key_name=comment.key().name(),
    #                source=self.sources[1], dest=self.dests[0])
    # self.assertRaises(AssertionError, diff.get_or_save)


class UserTest(testutil.ModelsTest):

  def test_no_logged_in_user(self):
    self.testbed.deactivate()
    self.setup_testbed(federated_identity='')
    self.assertEqual(None, users.get_current_user())
    self.assertEqual(None, User.get_current_user())
    self.assertEqual(None, User.get_or_insert_current_user(self.handler))

  def test_user(self):
    self.assertEqual(0, User.all().count())
    self.assertEqual(None, User.get_current_user())

    user = User.get_or_insert_current_user(self.handler)
    self.assertEqual(self.gae_user_id, user.key().name())
    self.assertEqual(['Registered new user.'], self.handler.messages)
    self.assert_entities_equal([User(key_name=self.gae_user_id)], User.all())

    # get_or_insert_current_user() again shouldn't add a message
    self.handler.messages = []
    user = User.get_or_insert_current_user(self.handler)
    self.assertEqual(self.gae_user_id, user.key().name())
    self.assertEqual([], self.handler.messages)
    self.assert_entities_equal([User(key_name=self.gae_user_id)], User.all())


class SiteTest(testutil.HandlerTest):

  def _test_create_new(self):
    FakeSite.create_new(self.handler)
    self.assertEqual(1, FakeSite.all().count())
    self.assertEqual(0, len(self.taskqueue_stub.GetTasks('poll')))

  def test_create_new(self):
    self.assertEqual(0, FakeSite.all().count())
    self._test_create_new()
    self.assertEqual(['Added FakeSite: fake/url'],
                     self.handler.messages)

  def test_create_new_already_exists(self):
    FakeSite.new(None).save()
    FakeSite.key_name_counter -= 1
    self._test_create_new()
    self.assertEqual(['Updated existing FakeSite: fake/url'],
                     self.handler.messages)


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

  def test_create_new_already_exists(self):
    FakeSource.new(None).save()
    FakeSource.key_name_counter -= 1
    self._test_create_new()

