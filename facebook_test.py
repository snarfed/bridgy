"""Unit tests for facebook.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import json
import testutil
import urllib2

from activitystreams import facebook_test as as_facebook_test
from activitystreams.oauth_dropins import facebook as oauth_facebook
from facebook import FacebookPage
import models

import webapp2


class FacebookPageTest(testutil.ModelsTest):

  def setUp(self):
    super(FacebookPageTest, self).setUp()
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
    self.assertEqual('http://graph.facebook.com/snarfed.org/picture?type=large',
                     page.picture)
    self.assertEqual('Ryan Barrett', page.name)
    self.assertEqual('snarfed.org', page.username)
    self.assertEqual('user', page.type)

  def test_get_activities(self):
    self.expect_urlopen(
      'https://graph.facebook.com/me/posts?offset=0&access_token=my_token',
      json.dumps({'data': [as_facebook_test.POST]}))
    self.expect_urlopen(
      'https://graph.facebook.com/me/photos/uploaded?access_token=my_token',
      json.dumps({'data': []}))
    self.mox.ReplayAll()

    page = FacebookPage.new(self.handler, auth_entity=self.auth_entity)
    self.assert_equals([as_facebook_test.ACTIVITY], page.get_activities())

  def test_revoked(self):
    self.expect_urlopen(
      'https://graph.facebook.com/me/posts?offset=0&access_token=my_token',
      json.dumps({'error': {'code': 190, 'error_subcode': 458}}), status=400)
    self.mox.ReplayAll()

    page = FacebookPage.new(self.handler, auth_entity=self.auth_entity)
    self.assertRaises(models.DisableSource, page.get_activities)

  def test_other_error(self):
    self.expect_urlopen(
      'https://graph.facebook.com/me/posts?offset=0&access_token=my_token',
      json.dumps({'error': {'code': 190, 'error_subcode': 789}}), status=400)
    self.mox.ReplayAll()

    page = FacebookPage.new(self.handler, auth_entity=self.auth_entity)
    self.assertRaises(urllib2.HTTPError, page.get_activities)
