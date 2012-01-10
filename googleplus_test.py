#!/usr/bin/python
"""Unit tests for googleplus.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import mox
import testutil

import googleplus
from googleplus import GooglePlusComment, GooglePlusPage, GooglePlusService
import models
import tasks_test


class GooglePlusPageTest(testutil.ModelsTest):

  def setUp(self):
    super(GooglePlusPageTest, self).setUp()

    self.mox.StubOutWithMock(GooglePlusService, 'call')
    self.mox.StubOutWithMock(GooglePlusService, 'call_with_creds')

    googleplus.HARD_CODED_DEST = 'FakeDestination'
    self.user = models.User.get_or_insert_current_user(self.handler)
    self.handler.messages = []

    self.page = GooglePlusPage(key_name='2468',
                               gae_user_id=self.gae_user_id,
                               owner=self.user,
                               name='my full name',
                               url='http://my.g+/url',
                               picture='http://my.pic/small',
                               type='user',
                               )

    self.people_get_response = {
        'id': '2468',
        'displayName': 'my full name',
        'url': 'http://my.g+/url',
        'image': {'url': 'http://my.pic/small'},
        'type': 'person',
        }

    self.activities_list_response = {'items': [
        # no attachments
        {'object': {}},
        # no article attachment
        {'object': {'attachments': [{'objectType': 'note'}]}},
        # no matching dest
        {'object': {'attachments': [{'objectType': 'article',
                                     'url': 'http://no/matching/dest'}]}},
        # matches self.dests[1]
        {'object': {'attachments': [{'objectType': 'article',
                                     'url': 'http://dest1/post/url'}]},
         'id': '1',
         'url': 'http://source/post/1',
         },
        # matches self.dests[0]
        {'object': {'attachments': [{'objectType': 'article',
                                     'url': 'http://dest0/post/url'}]},
         'id': '2',
         'url': 'http://source/post/0',
         },
        ]}

    # TODO: unify with ModelsTest.setUp()
    self.comments = [
      GooglePlusComment(
        key_name='123',
        created=datetime.datetime.utcfromtimestamp(1.01),
        source=self.page,
        dest=self.dests[1],
        source_post_url='http://source/post/1',
        dest_post_url='http://dest1/post/url',
        author_name='fred',
        author_url='http://fred',
        content='foo',
        user_id='4',
        ),
      GooglePlusComment(
        key_name='789',
        created=datetime.datetime.utcfromtimestamp(2.01),
        source=self.page,
        dest=self.dests[0],
        source_post_url='http://source/post/0',
        dest_post_url='http://dest0/post/url',
        author_name='bob',
        author_url='http://bob',
        content='bar',
        user_id='5',
        )]
    self.sources[0].set_comments(self.comments)

    # (activity id, JSON response) pairs
    self.comments_list_responses = [
      ('1', {'items': [{
              'id': '123',
              'object': {'content': 'foo'},
              'actor': {'id': '4', 'displayName': 'fred', 'url': 'http://fred'},
              'published': '1970-01-01T00:00:01.01Z',
              }]}),
      ('2', {'items': [{
              'id': '789',
              'object': {'content': 'bar'},
              'actor': {'id': '5', 'displayName': 'bob', 'url': 'http://bob'},
              'published': '1970-01-01T00:00:02.01Z',
              }]}),
      ]

  def test_new(self):
    GooglePlusService.call('http placeholder', 'people.get', userId='me')\
        .AndReturn(self.people_get_response)
    self.mox.ReplayAll()

    self.assert_entities_equal(
      self.page,
      GooglePlusPage.new(self.handler, http='http placeholder'),
      ignore=['created'])

  def test_poll(self):
    GooglePlusService.call_with_creds(
      self.gae_user_id, 'activities.list', userId='me', collection='public',
      maxResults=100)\
      .AndReturn(self.activities_list_response)
    for activity_id, response in self.comments_list_responses:
      GooglePlusService.call_with_creds(
        self.gae_user_id, 'comments.list', activityId=activity_id, maxResults=100)\
        .AndReturn(response)
    self.mox.ReplayAll()

    got = self.page.poll()
    self.assert_entities_equal(self.comments, got)
