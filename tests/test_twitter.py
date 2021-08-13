"""Unit tests for twitter.py.
"""
import copy
import urllib.request, urllib.parse, urllib.error

from granary import twitter as gr_twitter
from granary.tests import test_twitter as gr_twitter_test
from granary.twitter import API_BASE, API_SEARCH, API_STATUS, SCRAPE_LIKES_URL
import oauth_dropins.twitter
import oauth_dropins.twitter_auth
from oauth_dropins.webutil.util import json_dumps, json_loads

import models
from . import testutil
import twitter
from twitter import Twitter


class TwitterTest(testutil.ModelsTest):

  def setUp(self):
    super(TwitterTest, self).setUp()
    oauth_dropins.twitter.TWITTER_APP_KEY = 'my_app_key'
    oauth_dropins.twitter.TWITTER_APP_SECRET = 'my_app_secret'
    self.auth_entity = oauth_dropins.twitter.TwitterAuth(
      id='my_string_id',
      token_key='my_key', token_secret='my_secret',
      user_json=json_dumps({'name': 'Ryan Barrett',
                            'screen_name': 'snarfed_org',
                            'description': 'something about me',
                            'profile_image_url': 'http://pi.ct/ure',
                            }))
    self.auth_entity.put()
    self.tw = Twitter.new(auth_entity=self.auth_entity)

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
    user = json_loads(self.auth_entity.user_json)
    user['profile_image_url_https'] = 'https://foo_normal.xyz'
    self.auth_entity.user_json = json_dumps(user)

    self.assertEqual('https://foo.xyz', Twitter.new(auth_entity=self.auth_entity).picture)

  def test_get_like(self):
    """get_like() should use the Response stored in the datastore."""
    like = {
      'objectType': 'activity',
      'verb': 'like',
      'id': 'tag:twitter.com,2013:222',
      'object': {'url': 'http://my/favorite'},
      }
    models.Response(id='tag:twitter.com,2013:000_favorited_by_222',
                    response_json=json_dumps(like)).put()
    self.assert_equals(like, self.tw.get_like('unused', '000', '222'))

  def test_get_like_fallback(self):
    """If there's no Response in the datastore, fall back to get_activities."""
    models.TWITTER_SCRAPE_HEADERS = {'x': 'y'}

    tweet = copy.deepcopy(gr_twitter_test.TWEET)
    tweet['favorite_count'] = 1

    self.expect_urlopen(API_BASE + API_STATUS % '100', json_dumps(tweet))
    self.expect_requests_get(SCRAPE_LIKES_URL % '100', gr_twitter_test.LIKES_SCRAPED,
                             headers={'x': 'y'})

    self.mox.ReplayAll()
    like = copy.deepcopy(gr_twitter_test.LIKE_OBJECTS[0])
    like['id'] = 'tag:twitter.com,2013:100_favorited_by_353'
    like['author']['id'] = 'tag:twitter.com,2013:ge'
    self.assert_equals(like, self.tw.get_like('unused', '100', '353'))

  def test_canonicalize_url(self):
    good = 'https://twitter.com/x/status/123'
    self.assertEqual(good, self.tw.canonicalize_url(good))
    self.assertEqual(good, self.tw.canonicalize_url(
      'https://twitter.com/x/statuses/123'))
    self.assertEqual(good, self.tw.canonicalize_url(
      'https://twitter.com/x/status/123/'))
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
      'id_str': '1', # yes, ignore http vs https for bar/baz
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
      'id_str': '3', # no, link domain is blocklisted
      'text': 'x https://t.co/xyz/abc z',
    }, {
      'id_str': '4', # no link
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
    self.expect_urlopen(API_BASE + API_SEARCH %
                        {'q': urllib.parse.quote_plus('bar/baz OR foo'), 'count': 50},
                        json_dumps({'statuses': results}))

    self.mox.ReplayAll()
    self.assert_equals(
      ['tag:twitter.com,2013:1', 'tag:twitter.com,2013:5', 'tag:twitter.com,2013:6'],
      [a['id'] for a in self.tw.search_for_links()])

  def test_search_for_links_no_urls(self):
    # only a blocklisted domain
    self.tw.domain_urls = ['https://t.co/xyz']
    self.tw.put()
    self.mox.ReplayAll()
    self.assert_equals([], self.tw.search_for_links())

  def test_is_private(self):
    self.assertFalse(self.tw.is_private())

    self.auth_entity.user_json = json_dumps({'protected': True})
    self.auth_entity.put()
    self.assertTrue(self.tw.is_private())

  def test_gr_source_username(self):
    self.assertEqual('snarfed_org', self.tw.gr_source.username)

  def test_load_blocklist_rate_limited(self):
    """If we get rate limited, we should use the partial result."""
    api_url = gr_twitter.API_BASE + gr_twitter.API_BLOCK_IDS % '-1'
    self.expect_urlopen(api_url, json_dumps({
      'ids': ['1', '2'],
      'next_cursor_str': '2',
    }))
    api_url = gr_twitter.API_BASE + gr_twitter.API_BLOCK_IDS % '2'
    self.expect_urlopen(api_url, status=429)

    self.mox.ReplayAll()
    self.tw.load_blocklist()
    self.assert_equals(['1', '2'], self.tw.blocked_ids)

