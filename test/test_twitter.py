"""Unit tests for twitter.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import copy
import json

import appengine_config
from granary.test import test_twitter as gr_twitter_test
import oauth_dropins
from oauth_dropins import twitter as oauth_twitter

import models
import testutil
from twitter import Twitter


class TwitterTest(testutil.ModelsTest):

  def setUp(self):
    super(TwitterTest, self).setUp()
    oauth_dropins.appengine_config.TWITTER_APP_KEY = 'my_app_key'
    oauth_dropins.appengine_config.TWITTER_APP_SECRET = 'my_app_secret'
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
    self.tw = Twitter.new(self.handler, auth_entity=self.auth_entity)

  def test_new(self):
    self.assertEqual(self.auth_entity, self.tw.auth_entity.get())
    self.assertEqual('my_key', self.tw.gr_source.access_token_key)
    self.assertEqual('my_secret', self.tw.gr_source.access_token_secret)
    self.assertEqual('snarfed_org', self.tw.key.string_id())
    self.assertEqual('https://twitter.com/snarfed_org/profile_image?size=original',
                     self.tw.picture)
    self.assertEqual('Ryan Barrett', self.tw.name)
    self.assertEqual('https://twitter.com/snarfed_org', self.tw.url)
    self.assertEqual('https://twitter.com/snarfed_org', self.tw.silo_url())
    self.assertEqual('tag:twitter.com,2013:snarfed_org', self.tw.user_tag_id())
    self.assertEqual('snarfed_org (Twitter)', self.tw.label())

  def test_new_massages_profile_image(self):
    """We should use profile_image_url_https and drop '_normal' if possible."""
    user = json.loads(self.auth_entity.user_json)
    user['profile_image_url_https'] = 'https://foo_normal.xyz'
    self.auth_entity.user_json = json.dumps(user)

    self.assertEqual('https://twitter.com/snarfed_org/profile_image?size=original',
                     Twitter.new(self.handler, auth_entity=self.auth_entity).picture)

  # def test_find_user_mentions(self):
  #   mention_1 = {
  #     'objectType': 'person',
  #     'id': 'tag:twitter.com,2013:snarfed_org',
  #   }
  #   mention_2 = copy.copy(mention_1)
  #   activity = {
  #     'object': {
  #       'tags': [
  #         mention_1,
  #         {'objectType': 'person', 'id': 'tag:twitter.com,2013:bob_org'},
  #         mention_2,
  #       ],
  #     },
  #   }
  #   self.assertEquals([mention_1, mention_2],
  #                     Twitter.find_user_mentions(activity))

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
    self.assert_equals(like, self.tw.get_like('unused', '000', '222'))

  def test_get_like_fallback(self):
    """If there's no Response in the datastore, fall back to get_activities."""
    tweet = copy.deepcopy(gr_twitter_test.TWEET)
    tweet['favorite_count'] = 1

    self.expect_urlopen(
      'https://api.twitter.com/1.1/statuses/show.json?id=100&include_entities=true',
      json.dumps(tweet))
    self.expect_urlopen('https://twitter.com/i/activity/favorited_popup?id=100',
      json.dumps({'htmlUsers': gr_twitter_test.FAVORITES_HTML}))

    self.mox.ReplayAll()
    self.assert_equals(gr_twitter_test.LIKES_FROM_HTML[0],
                       self.tw.get_like('unused', '100', '353'))

  def test_canonicalize_syndication_url(self):
    for url in (
        'http://www.twitter.com/username/012345',
        'https://www.twitter.com/username/012345',
        'http://twitter.com/username/012345',
    ):
      self.assertEqual('https://twitter.com/username/012345',
                       self.tw.canonicalize_syndication_url(url))
