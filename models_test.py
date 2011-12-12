#!/usr/bin/python
"""Unit tests for models.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import collections
import datetime
import unittest

from models import Comment, Destination, Source, User
import testutil

from google.appengine.api import users
from google.appengine.ext import db


class FakeBase(db.Model):
  """Not thread safe.
  """

  key_name_counter = 0

  @classmethod
  def new(cls):
    FakeBase.key_name_counter += 1
    inst = cls(key_name=str(FakeBase.key_name_counter))
    inst.save()
    return inst

  def type_display_name(self):
    return self.__class__.__name__


class FakeDestination(FakeBase, Destination):
  """  Attributes:
    comments: dict mapping FakeDestination string key to list of Comment entities
  """

  comments = collections.defaultdict(list)

  def add_comment(self, comment):
    FakeDestination.comments[str(self.key())].append(comment)

  def get_comments(self):
    return FakeDestination.comments[str(self.key())]


class FakeSource(FakeBase, Source):
  """Attributes:
    comments: dict mapping FakeSource string key to list of Comments to be
      returned by poll()
  """
  comments = {}

  def poll(self):
    return FakeSource.comments[str(self.key())]

  def set_comments(self, comments):
    FakeSource.comments[str(self.key())] = comments


class ModelsTest(testutil.HandlerTest):
  """Sets up some test sources, destinations, and comments.
  """

  def setUp(self):
    super(ModelsTest, self).setUp()
    self.setup_testbed()

    self.sources = [FakeSource.new(), FakeSource.new()]
    self.dests = [FakeDestination.new(), FakeDestination.new()]
    now = datetime.datetime.now()

    properties = {
      'source': self.sources[0],
      'created': now,
      'source_post_url': 'http://source/post/url',
      'source_comment_url': 'http://source/comment/url',
      'dest_post_url': 'http://dest/post/url',
      'dest_comment_url': 'http://dest/comment/url',
      'content': 'foo',
      'author_name': 'me',
      'author_url': 'http://me',
      }
    self.comments = [
      Comment(key_name='a', dest=self.dests[0], **properties),
      Comment(key_name='b', dest=self.dests[1], **properties),
      ]
    self.sources[0].set_comments(self.comments)


class CommentTest(ModelsTest):

  def test_get_or_save(self):
    comment = self.comments[0]

    # new
    saved = comment.get_or_save()
    self.assertTrue(saved.is_saved())
    self.assertEqual(comment.key(), saved.key())
    self.assertEqual(comment.source, saved.source)
    self.assertEqual(comment.dest, saved.dest)

    # existing
    same = saved.get_or_save()
    self.assertEqual(saved.source.key(), same.source.key())
    self.assertEqual(saved.dest.key(), same.dest.key())

    # different source and dest
    diff = Comment(key_name=comment.key().name(),
                   source=self.sources[0], dest=self.dests[1])
    self.assertRaises(AssertionError, diff.get_or_save)
    diff = Comment(key_name=comment.key().name(),
                   source=self.sources[1], dest=self.dests[0])
    self.assertRaises(AssertionError, diff.get_or_save)


class UserTest(testutil.HandlerTest):

  def test_no_logged_in_user(self):
    self.testbed.deactivate()
    self.setup_testbed(federated_identity='')
    self.assertEqual(None, users.get_current_user())
    self.assertEqual(None, User.get_current_user())
    self.assertEqual(None, User.get_or_insert_current_user(self.handler))

  def test_gae_user(self):
    self._test_user('123', user_email='foo@bar.com', user_id='123',
                    federated_identity='')

  def test_openid_user(self):
    self._test_user('foo.com/bar', federated_identity='foo.com/bar')

  def _test_user(self, expected_key_name, **setup_env):
    self.testbed.deactivate()
    self.setup_testbed(**setup_env)
    self.assertEqual(0, User.all().count())
    self.assertEqual(None, User.get_current_user())

    user = User.get_or_insert_current_user(self.handler)
    self.assertEqual(expected_key_name, user.key().name())
    self.assertEqual(['Registered new user.'], self.handler.messages)
    self.assert_entities_equal([User(key_name=expected_key_name)], User.all())

    # get_or_insert_current_user() again shouldn't add a message
    self.handler.messages = []
    user = User.get_or_insert_current_user(self.handler)
    self.assertEqual(expected_key_name, user.key().name())
    self.assertEqual([], self.handler.messages)
    self.assert_entities_equal([User(key_name=expected_key_name)], User.all())
