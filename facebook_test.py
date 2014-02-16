"""Unit tests for facebook.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import json
import urllib
import urllib2

import appengine_config


from activitystreams import facebook_test as as_facebook_test
from activitystreams.oauth_dropins import facebook as oauth_facebook
from facebook import FacebookPage
import models
import testutil


class FacebookPageTest(testutil.ModelsTest):

  def setUp(self):
    super(FacebookPageTest, self).setUp()
    appengine_config.FACEBOOK_APP_ID = 'my_app_id'
    appengine_config.FACEBOOK_APP_SECRET = 'my_app_secret'
    self.handler.messages = []
    self.auth_entity = oauth_facebook.FacebookAuth(
      id='my_string_id', auth_code='my_code', access_token_str='my_token',
      user_json=json.dumps({'id': '212038',
                            'name': 'Ryan Barrett',
                            'username': 'snarfed.org',
                            'bio': 'something about me',
                            'type': 'user',
                            }))
    self.auth_entity.put()

  def test_new(self):
    page = FacebookPage.new(self.handler, auth_entity=self.auth_entity)
    self.assertEqual(self.auth_entity, page.auth_entity.get())
    self.assertEqual('my_token', page.as_source.access_token)
    self.assertEqual('212038', page.key.id())
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
      json.dumps({'data': [as_facebook_test.POST]}))
    self.expect_urlopen(
      'https://graph.facebook.com/me/events?access_token=my_token',
      json.dumps({'data': [as_facebook_test.EVENT]}))
    self.expect_urlopen(
      'https://graph.facebook.com/145304994?access_token=my_token',
      json.dumps(as_facebook_test.EVENT))
    self.expect_urlopen(
      'https://graph.facebook.com/145304994/invited?access_token=my_token',
      json.dumps({'data': as_facebook_test.RSVPS}))
    self.mox.ReplayAll()

    page = FacebookPage.new(self.handler, auth_entity=self.auth_entity)
    self.assert_equals([as_facebook_test.ACTIVITY,
                        as_facebook_test.ACTIVITY,
                        as_facebook_test.EVENT_ACTIVITY_WITH_ATTENDEES,
                        ], page.get_activities())

  def test_revoked(self):
    self.expect_urlopen(
      'https://graph.facebook.com/me/posts?offset=0&access_token=my_token',
      json.dumps({'error': {'code': 190, 'error_subcode': 458}}), status=400)
    self.mox.ReplayAll()

    page = FacebookPage.new(self.handler, auth_entity=self.auth_entity)
    self.assertRaises(models.DisableSource, page.get_activities)

  def test_expired_sends_notification(self):
    self.expect_urlopen(
      'https://graph.facebook.com/me/posts?offset=0&access_token=my_token',
      json.dumps({'error': {'code': 190, 'error_subcode': 463}}), status=400)

    params = {
      'template': "Brid.gy's access to your account has expired. Click here to renew it now!",
       'href': 'https://www.brid.gy/facebook/start',
      'access_token': 'my_app_id|my_app_secret',
      }
    self.expect_urlopen('https://graph.facebook.com/212038/notifications', '',
                        data=urllib.urlencode(params))
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
