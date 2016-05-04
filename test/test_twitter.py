"""Unit tests for twitter.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import copy
import json
import urllib

import appengine_config
from granary import twitter as gr_twitter
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
    self.assertEqual('http://pi.ct/ure', self.tw.picture)
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

    self.assertEqual('https://foo.xyz', Twitter.new(self.handler, auth_entity=self.auth_entity).picture)

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

  def test_canonicalize_url(self):
    good = 'https://twitter.com/x/status/123'
    self.assertEqual(good, self.tw.canonicalize_url(good))
    self.assertEqual(good, self.tw.canonicalize_url(
      'https://twitter.com/x/statuses/123'))
    self.assertIsNone(self.tw.canonicalize_url(
      'https://twitter.com/x?protected_redirect=true'))

  def test_search_for_links(self):
    """https://github.com/snarfed/bridgy/issues/565"""
    self.tw.domain_urls = ['http://foo/', 'http://bar/baz', 'https://t.co/xyz']
    self.tw.put()

    results = [{
      'id_str': '0', # no link
      'text': 'x foo/ y /bar/baz z',
    }, {
      'id_str': '1', # no link
      'text': 'no link here',
      'entities': {'urls': [{'expanded_url': 'http://bar'},
                            {'expanded_url': 'https://bar/baz'},
      ]},
    }, {
      'id_str': '2', # no, retweet
      'text': 'a http://bar/baz ok',
      'retweeted_status': {
        'id_str': '456',
        'text': 'a http://bar/baz ok',
      },
    }, {
      'id_str': '3', # no, link domain is blacklisted
      'text': 'x https://t.co/xyz/abc z',
    }, {
      'id_str': '4', # yes
      'text': 'x http://bar/baz z',
    }, {
      'id_str': '5', # yes
      'text': 'no link here',
      'entities': {'urls': [{'expanded_url': 'http://foo/x?y'}]},
    }, {
      'id_str': '6', # yes
      'text': 'a link http://bar/baz here',
      'entities': {'urls': [{'expanded_url': 'http://foo/'},
                            {'expanded_url': 'http://other'}]},
    }]
    self.expect_urlopen(gr_twitter.API_BASE + gr_twitter.API_SEARCH %
                        {'q': urllib.quote_plus('"foo" OR "bar/baz"'), 'count': 50},
                        json.dumps({'statuses': results}))

    self.mox.ReplayAll()
    self.assert_equals(
      ['tag:twitter.com,2013:4', 'tag:twitter.com,2013:5', 'tag:twitter.com,2013:6'],
      [a['id'] for a in self.tw.search_for_links()])

  def test_search_for_links_no_urls(self):
    # only a blacklisted domain
    self.tw.domain_urls = ['https://t.co/xyz']
    self.tw.put()
    self.mox.ReplayAll()
    self.assert_equals([], self.tw.search_for_links())

  def test_is_private(self):
    self.assertFalse(self.tw.is_private())

    self.auth_entity.user_json = json.dumps({'protected': True})
    self.auth_entity.put()
    self.assertTrue(self.tw.is_private())
