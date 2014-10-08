"""Unit tests for twitter.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import copy
import datetime
import json
import testutil
import urllib

from activitystreams import twitter_test as as_twitter_test
from activitystreams.oauth_dropins import twitter as oauth_twitter
import appengine_config
import models
import twitter
import tweepy
from twitter import Twitter


class TwitterTest(testutil.ModelsTest):

  def setUp(self):
    super(TwitterTest, self).setUp()
    self.handler.messages = []
    self.auth_entity = oauth_twitter.TwitterAuth(
      id='my_string_id',
      token_key='my_key', token_secret='my_secret',
      user_json=json.dumps({'name': 'Ryan Barrett',
                            'screen_name': 'snarfed_org',
                            'description': 'something about me',
                            'profile_image_url': 'http://pi.ct/ure',
                            }))
    self.auth_entity.put()

  def test_new(self):
    tw = Twitter.new(self.handler, auth_entity=self.auth_entity)
    self.assertEqual(self.auth_entity, tw.auth_entity.get())
    self.assertEqual('my_key', tw.as_source.access_token_key)
    self.assertEqual('my_secret', tw.as_source.access_token_secret)
    self.assertEqual('snarfed_org', tw.key.string_id())
    self.assertEqual('http://pi.ct/ure', tw.picture)
    self.assertEqual('Ryan Barrett', tw.name)
    self.assertEqual('https://twitter.com/snarfed_org', tw.url)
    self.assertEqual('https://twitter.com/snarfed_org', tw.silo_url())

  def test_new_massages_profile_image(self):
    """We should use profile_image_url_https and drop '_normal' if possible."""
    user = json.loads(self.auth_entity.user_json)
    user['profile_image_url_https'] = 'https://foo_normal.xyz'
    self.auth_entity.user_json = json.dumps(user)

    tw = Twitter.new(self.handler, auth_entity=self.auth_entity)
    self.assertEqual('https://foo.xyz', tw.picture)

  def test_get_activities(self):
    self.expect_urlopen('https://api.twitter.com/1.1/statuses/user_timeline.json?'
                        'include_entities=true&count=0',
      json.dumps([as_twitter_test.TWEET]))
    self.mox.ReplayAll()

    tw = Twitter.new(self.handler, auth_entity=self.auth_entity)
    self.assert_equals([as_twitter_test.ACTIVITY], tw.get_activities())

  def test_get_like(self):
    """get_like() should use the Response stored in the datastore."""
    like = {
      'objectType': 'activity',
      'verb': 'like',
      'id': 'tag:twitter.com,2013:222',
      'object': {'url': 'http://my/favorite'},
      }
    models.Response(id='tag:twitter.com,2013:000_favorited_by_222',
                    response_json=json.dumps(like)).put()

    tw = Twitter.new(self.handler, auth_entity=self.auth_entity)
    self.assert_equals(like, tw.get_like('unused', '000', '222'))

  def test_get_like_fallback(self):
    """If there's no Response in the datastore, fall back to get_activities."""
    tweet = copy.deepcopy(as_twitter_test.TWEET)
    tweet['favorite_count'] = 1

    self.expect_urlopen(
      'https://api.twitter.com/1.1/statuses/show.json?id=100&include_entities=true',
      json.dumps(tweet))
    self.expect_urlopen('https://twitter.com/i/activity/favorited_popup?id=100',
      json.dumps({'htmlUsers': as_twitter_test.FAVORITES_HTML}))

    self.mox.ReplayAll()
    tw = Twitter.new(self.handler, auth_entity=self.auth_entity)
    self.assert_equals(as_twitter_test.LIKES_FROM_HTML[0],
                       tw.get_like('unused', '100', '353'))

  def test_canonicalize_syndication_url(self):
    tw = Twitter.new(self.handler, auth_entity=self.auth_entity)
    for url in (
        'http://www.twitter.com/username/012345',
        'https://www.twitter.com/username/012345',
        'http://twitter.com/username/012345',
    ):
      self.assertEqual('https://twitter.com/username/012345',
                       tw.canonicalize_syndication_url(url))

  def test_registration_callback(self):
    """Run through an authorization back and forth and make sure that
    the callback makes it all the way through.
    """
    class FakeAuthHandler:
      def __init__(self):
        self.request_token = {
          'oauth_token': 'fake-oauth-token',
          'oauth_token_secret': 'fake-oauth-token-secret',
        }

      def get_authorization_url(self, *args, **kwargs):
        return 'http://fake/auth/url'

      def get_access_token(self, *args, **kwargs):
        return 'fake-access-token', 'fake-access-token-secret'

    self.mox.StubOutWithMock(tweepy, 'OAuthHandler')

    encoded_state = urllib.quote_plus(
      '{"callback":"http://withknown.com/bridgy_callback",'
      '"feature":"listen","operation":"add"}')

    tweepy.OAuthHandler(
      appengine_config.TWITTER_APP_KEY,
      appengine_config.TWITTER_APP_SECRET,
      'http://localhost/twitter/add?state=' + encoded_state
    ).AndReturn(FakeAuthHandler())

    tweepy.OAuthHandler(
      appengine_config.TWITTER_APP_KEY,
      appengine_config.TWITTER_APP_SECRET
    ).AndReturn(FakeAuthHandler())

    self.expect_urlopen(
      u'https://api.twitter.com/1.1/account/verify_credentials.json',
      json.dumps(as_twitter_test.USER))

    self.expect_requests_get(
      u'https://snarfed.org/',
      response='<html><link rel="webmention" href="/webmention"></html>',
      verify=False)

    self.mox.ReplayAll()

    resp = twitter.application.get_response(
      '/twitter/start', method='POST', body=urllib.urlencode({
        'feature': 'listen',
        'callback': 'http://withknown.com/bridgy_callback',
      }))

    self.assert_equals(302, resp.status_code)
    self.assert_equals('http://fake/auth/url', resp.headers['location'])

    resp = twitter.application.get_response(
      '/twitter/add?state=' + encoded_state +
      '&oauth_token=fake-oauth-token&oauth_token_secret=fake-oauth-token-secret')

    self.assert_equals(302, resp.status_code)
    self.assert_equals('http://withknown.com/bridgy_callback',
                       resp.headers['location'])

    tw = Twitter.query().get()
    self.assert_(tw)
    self.assert_equals(as_twitter_test.USER['name'], tw.name)
    self.assert_equals([u'listen'], tw.features)
