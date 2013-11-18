"""Unit tests for facebook.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import json
import logging
import mox
import re
import testutil
import urllib
import urlparse

from activitystreams.oauth_dropins import facebook as oauth_facebook
import facebook
from facebook import FacebookPage
import models

import webapp2


class FacebookPageTest(testutil.ModelsTest):

  def setUp(self):
    super(FacebookPageTest, self).setUp()
    # self.user = models.User.get_or_insert_current_user(self.handler)
    self.handler.messages = []
    self.auth_entity = oauth_facebook.FacebookAuth(
      key_name='my_key_name', auth_code='my_code', access_token_str='my_token',
      user_json=json.dumps({'id': '212038',
                            'name': 'Ryan Barrett',
                            'username': 'snarfed.org',
                            'bio': 'something about me',
                            'type': 'user',
                            }))

  def test_new(self):
    page = FacebookPage.new(self.handler, auth_entity=self.auth_entity)
    self.assertEqual(self.auth_entity, page.auth_entity)
    self.assertEqual('my_token', page.as_source.access_token)
    self.assertEqual('212038', page.key().name())
    self.assertEqual('http://graph.facebook.com/snarfed.org/picture', page.picture)
    self.assertEqual('Ryan Barrett', page.name)
    self.assertEqual('snarfed.org', page.username)
    self.assertEqual('user', page.type)

  def test_get_comments(self):
    self.expect_urlopen(
      'https://graph.facebook.com/me/home?offset=0&limit=0&access_token=my_token',
      json.dumps({'data': [{
              'id': '212038_000',
              'comments': {'count': 1,
                           'data': [{'id': '1_2_3', 'message': 'foo'}]}
              }, {
              'id': '212038_001',
              'comments': {'count': 2,
                           'data': [{'id': '4_5_6', 'message': 'bar'},
                                    {'id': '7_8_9', 'message': 'baz'}]}
              }]}))
    self.mox.ReplayAll()

    page = FacebookPage.new(self.handler, auth_entity=self.auth_entity)
    comments = list(page.get_comments())

    self.assertEqual(3, len(comments))

    self.assert_equals({
        'objectType': 'note',
        'id': 'tag:facebook.com,2013:212038_000',
        'url': 'http://facebook.com/212038/posts/000',
        }, json.loads(comments[0].post_as_json))
    self.assert_equals({
          'objectType': 'comment',
          'id': 'tag:facebook.com,2013:1_2_3',
          'url': 'http://facebook.com/2?comment_id=3',
          'inReplyTo': {'id': 'tag:facebook.com,2013:1_2'},
          'content': 'foo',
          }, json.loads(comments[0].comment_as_json))

    for c in comments[1:]:
      self.assert_equals({
          'objectType': 'note',
          'id': 'tag:facebook.com,2013:212038_001',
          'url': 'http://facebook.com/212038/posts/001',
          }, json.loads(c.post_as_json))

    self.assert_equals({
          'objectType': 'comment',
          'id': 'tag:facebook.com,2013:4_5_6',
          'url': 'http://facebook.com/5?comment_id=6',
          'inReplyTo': {'id': 'tag:facebook.com,2013:4_5'},
          'content': 'bar',
          }, json.loads(comments[1].comment_as_json))
    self.assert_equals({
          'objectType': 'comment',
          'id': 'tag:facebook.com,2013:7_8_9',
          'url': 'http://facebook.com/8?comment_id=9',
          'inReplyTo': {'id': 'tag:facebook.com,2013:7_8'},
          'content': 'baz',
          }, json.loads(comments[2].comment_as_json))
