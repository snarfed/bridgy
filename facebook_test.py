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

  def test_get_activities(self):
    self.expect_urlopen(
      'https://graph.facebook.com/me/posts?offset=0&access_token=my_token',
      json.dumps({'data': [{
              'id': '212038_000',
              'comments': {'count': 1,
                           'data': [{'id': '2_3', 'message': 'foo'}]}
              }]}))
    self.mox.ReplayAll()

    page = FacebookPage.new(self.handler, auth_entity=self.auth_entity)
    self.assert_equals([{
          'verb': 'post',
          'id': 'tag:facebook.com,2013:000',
          'url': 'http://facebook.com/000',
          'title': 'Unknown posted a unknown.',
          'object': {
            'objectType': 'note',
            'id': 'tag:facebook.com,2013:000',
            'url': 'http://facebook.com/000',
            'replies': {
              'items': [{
                  'objectType': 'comment',
                  'id': 'tag:facebook.com,2013:2_3',
                  'url': 'http://facebook.com/2?comment_id=3',
                  'inReplyTo': {'id': 'tag:facebook.com,2013:2'},
                  'content': 'foo',
                  }],
              'totalItems': 1,
              },
            },
          }], page.get_activities())
