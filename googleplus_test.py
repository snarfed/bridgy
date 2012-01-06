#!/usr/bin/python
"""Unit tests for googleplus.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import testutil

import googleplus
from googleplus import GooglePlusPage, GooglePlusComment
import models
import tasks_test


class GooglePlusPageTest(testutil.ModelsTest):

  def setUp(self):
    super(GooglePlusPageTest, self).setUp()

    googleplus.HARD_CODED_DEST = 'FakeDestination'
    self.user = models.User.get_or_insert_current_user(self.handler)
    self.handler.messages = []

    self.person = {
        'id': '2468',
        'displayName': 'my full name',
        'url': 'http://my.g+/url',
        'image': {'url': 'http://my.pic/small'},
        'type': 'person',
        }

    self.page = GooglePlusPage(key_name='2468',
                               owner=self.user,
                               name='my full name',
                               url='http://my.g+/url',
                               pic_small='http://my.pic/small',
                               type='user',
                               )

    # # TODO: unify with ModelsTest.setUp()
    # self.comments = [
    #   GooglePlusComment(
    #     key_name='123',
    #     created=datetime.datetime.utcfromtimestamp(1),
    #     source=self.page,
    #     dest=self.dests[1],
    #     source_post_url='https://www.facebook.com/permalink.php?story_fbid=1&id=4',
    #     dest_post_url='http://dest1/post/url',
    #     author_name='fred',
    #     author_url='http://fred',
    #     content='foo',
    #     fb_fromid=4,
    #     fb_username='',
    #     fb_object_id=1,
    #     ),
    #   GooglePlusComment(
    #     key_name='789',
    #     created=datetime.datetime.utcfromtimestamp(2),
    #     source=self.page,
    #     dest=self.dests[0],
    #     source_post_url='https://www.facebook.com/permalink.php?story_fbid=2&id=5',
    #     dest_post_url='http://dest0/post/url',
    #     author_name='bob',
    #     author_url='http://bob',
    #     content='bar',
    #     fb_fromid=5,
    #     fb_username='',
    #     fb_object_id=2,
    #     ),
    #   ]

    # self.sources[0].set_comments(self.comments)

    self.task_name = str(self.page.key()) + '_1970-01-01-00-00-00'

  def _test_new(self):
    got = GooglePlusPage.new(self.person, self.handler)
    self.assert_entities_equal(self.page, got, ignore=['created'])
    self.assert_entities_equal([self.page], GooglePlusPage.all(), ignore=['created'])

    tasks = self.taskqueue_stub.GetTasks('poll')
    self.assertEqual(1, len(tasks))
    self.assertEqual(self.task_name, tasks[0]['name'])
    self.assertEqual('/_ah/queue/poll', tasks[0]['url'])

  def test_new(self):
    self._test_new()
    self.assertEqual(self.handler.messages, ['Added Google+ page: my full name'])

  def test_new_already_exists(self):
    self.page.save()
    self._test_new()
    self.assertEqual(self.handler.messages,
                     ['Updated existing Google+ page: my full name'])

  def test_new_user_already_owns(self):
    self.user.sources = [self.page.key()]
    self.user.save()
    self._test_new()

  # def test_poll(self):
  #   # note that json requires double quotes. :/
  #   self.expect_fql('SELECT post_fbid, ', [
  #       {'post_fbid': '123', 'object_id': 1, 'fromid': 4,
  #        'username': '', 'time': 1, 'text': 'foo'},
  #       {'post_fbid': '789', 'object_id': 2, 'fromid': 5,
  #        'username': '', 'time': 2, 'text': 'bar'},
  #       ])
  #   self.expect_fql('SELECT link_id, url FROM link ', [
  #       {'link_id': 1, 'url': 'http://dest1/post/url'},
  #       {'link_id': 2, 'url': 'http://dest0/post/url'},
  #       ])
  #   self.expect_fql('SELECT id, name, url FROM profile ', [
  #       {'id': 4, 'name': 'fred', 'url': 'http://fred'},
  #       {'id': 5, 'name': 'bob', 'url': 'http://bob'},
  #       ])

  #   self.mox.ReplayAll()
  #   got = self.page.poll()
  #   self.assert_entities_equal(self.comments, got)
