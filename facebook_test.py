"""Unit tests for facebook.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import copy
import datetime
import json
import re
import urllib
import urllib2

import appengine_config

import activitystreams
from activitystreams import facebook_test as as_facebook_test
from activitystreams.oauth_dropins import facebook as oauth_facebook
from facebook import FacebookPage
import facebook
import models
import tasks
import testutil


class FacebookPageTest(testutil.ModelsTest):

  def setUp(self):
    super(FacebookPageTest, self).setUp()
    for config in (appengine_config, activitystreams.appengine_config,
                   activitystreams.oauth_dropins.appengine_config):
      setattr(config, 'FACEBOOK_APP_ID', 'my_app_id')
      setattr(config, 'FACEBOOK_APP_SECRET', 'my_app_secret')

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
    self.fb = FacebookPage.new(self.handler, auth_entity=self.auth_entity,
                               features=['listen'])
    self.fb.put()

    self.post_activity = copy.deepcopy(as_facebook_test.ACTIVITY)
    fb_id_and_url = {
      'id': 'tag:facebook.com,2013:222', # this is fb_object_id
      'url': 'https://facebook.com/212038/posts/222',
      }
    self.post_activity.update(fb_id_and_url)
    self.post_activity['object'].update(fb_id_and_url)

  def test_new(self):
    self.assertEqual(self.auth_entity, self.fb.auth_entity.get())
    self.assertEqual('my_token', self.fb.as_source.access_token)
    self.assertEqual('212038', self.fb.key.id())
    self.assertEqual('http://graph.facebook.com/snarfed.org/picture?type=large',
                     self.fb.picture)
    self.assertEqual('Ryan Barrett', self.fb.name)
    self.assertEqual('snarfed.org', self.fb.username)
    self.assertEqual('user', self.fb.type)
    self.assertEqual('https://facebook.com/snarfed.org', self.fb.silo_url())

  def test_get_activities(self):
    owned_event = copy.deepcopy(as_facebook_test.EVENT)
    owned_event['id'] = '888'
    owned_event['owner']['id'] = '212038'
    self.expect_urlopen(
      'https://graph.facebook.com/me/posts?offset=0&access_token=my_token',
      json.dumps({'data': [as_facebook_test.POST]}))
    self.expect_urlopen(
      'https://graph.facebook.com/me/photos/uploaded?access_token=my_token',
      json.dumps({'data': [as_facebook_test.POST]}))
    self.expect_urlopen(
      'https://graph.facebook.com/me/events?access_token=my_token',
      json.dumps({'data': [as_facebook_test.EVENT, owned_event]}))
    self.expect_urlopen(
      re.compile('^https://graph.facebook.com/145304994.+'),
      json.dumps(as_facebook_test.EVENT))
    self.expect_urlopen(
      re.compile('^https://graph.facebook.com/888\?.+'),
      json.dumps(owned_event))
    self.expect_urlopen(
      'https://graph.facebook.com/888/invited?access_token=my_token',
      json.dumps({'data': as_facebook_test.RSVPS}))
    self.mox.ReplayAll()

    event_activity = self.fb.as_source.event_to_activity(owned_event)
    for k in 'attending', 'notAttending', 'maybeAttending', 'invited':
      event_activity['object'][k] = as_facebook_test.EVENT_OBJ_WITH_ATTENDEES[k]
    self.assert_equals([self.post_activity, as_facebook_test.ACTIVITY, event_activity],
                       self.fb.get_activities())

  def test_get_activities_post_and_photo_duplicates(self):
    self.assertEqual(as_facebook_test.POST['object_id'],
                        as_facebook_test.PHOTO['id'])
    self.expect_urlopen(
      'https://graph.facebook.com/me/posts?offset=0&access_token=my_token',
      json.dumps({'data': [as_facebook_test.POST]}))
    self.expect_urlopen(
      'https://graph.facebook.com/me/photos/uploaded?access_token=my_token',
      json.dumps({'data': [as_facebook_test.PHOTO]}))
    self.expect_urlopen(
      'https://graph.facebook.com/me/events?access_token=my_token',
      json.dumps({}))
    self.mox.ReplayAll()

    self.assert_equals([self.post_activity], self.fb.get_activities())

  def test_revoked(self):
    self.expect_urlopen(
      'https://graph.facebook.com/me/posts?offset=0&access_token=my_token',
      json.dumps({'error': {'code': 190, 'error_subcode': 458}}), status=400)
    self.mox.ReplayAll()

    self.assertRaises(models.DisableSource, self.fb.get_activities)

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

    self.assertRaises(models.DisableSource, self.fb.get_activities)

  def test_other_error(self):
    self.expect_urlopen(
      'https://graph.facebook.com/me/posts?offset=0&access_token=my_token',
      json.dumps({'error': {'code': 190, 'error_subcode': 789}}), status=400)
    self.mox.ReplayAll()

    self.assertRaises(urllib2.HTTPError, self.fb.get_activities)

  def test_other_error_not_json(self):
    """If an error body isn't JSON, we should raise the original exception."""
    self.expect_urlopen(
      'https://graph.facebook.com/me/posts?offset=0&access_token=my_token',
      'not json', status=400)
    self.mox.ReplayAll()

    self.assertRaises(urllib2.HTTPError, self.fb.get_activities)

  def test_canonicalize_syndication_url(self):
    for expected, input in (
      ('https://facebook.com/212038/posts/314159',
       'http://facebook.com/snarfed.org/posts/314159'),
      ('https://facebook.com/212038/posts/314159',
       'https://www.facebook.com/snarfed.org/photos.php?fbid=314159'),
      ('https://facebook.com/212038/posts/10101299919362973',
       'https://www.facebook.com/photo.php?fbid=10101299919362973&set=a.995695740593.2393090.212038&type=1&theater'),
      ('https://facebook.com/212038/posts/314159',
       'https://facebook.com/permalink.php?story_fbid=314159&id=212038'),
      ('https://facebook.com/212038/posts/314159',
       'https://facebook.com/permalink.php?story_fbid=314159&amp;id=212038'),
      # make sure we don't touch user.name when it appears elsewhere in the url
      ('https://facebook.com/25624/posts/snarfed.org',
       'http://www.facebook.com/25624/posts/snarfed.org')):
      self.assertEqual(expected, self.fb.canonicalize_syndication_url(input))

  def test_photo_syndication_url(self):
    """End to end test with syndication URL with FB object id instead of post id.

    Background in https://github.com/snarfed/bridgy/issues/189
    """
    self.fb.domain_urls=['http://author/url']
    self.fb.last_hfeed_fetch = datetime.datetime.utcnow()
    self.fb.put()

    # Facebook API calls
    self.expect_urlopen(
      'https://graph.facebook.com/me/posts?offset=0&limit=50&access_token=my_token',
      json.dumps({'data': [as_facebook_test.POST]}))
    self.expect_urlopen(
      'https://graph.facebook.com/me/photos/uploaded?access_token=my_token', '{}')
    self.expect_urlopen(
      'https://graph.facebook.com/me/events?access_token=my_token', '{}')

    # posse post discovery
    self.expect_requests_get('http://author/url', """
    <html>
      <div class="h-entry">
        <a class="u-url" href="http://my.orig/post"></a>
      </div>
    </html>""")

    self.assertNotIn('222', as_facebook_test.POST['id'])
    self.assertEquals('222', as_facebook_test.POST['object_id'])
    self.expect_requests_get('http://my.orig/post', """
    <html class="h-entry">
      <a class="u-syndication" href="https://www.facebook.com/photo.php?fbid=222&set=a.995695740593.2393090.212038&type=1&theater'"></a>
    </html>""")

    self.mox.ReplayAll()

    resp = tasks.application.get_response(
      '/_ah/queue/poll', method='POST', body=urllib.urlencode({
          'source_key': self.fb.key.urlsafe(),
          'last_polled': '1970-01-01-00-00-00',
          }))
    self.assertEqual(200, resp.status_int)
    for resp in models.Response.query():
      self.assertEqual(['http://my.orig/post'], resp.unsent)
