"""Unit tests for reddit.py.
"""
from granary.tests import test_reddit as gr_reddit_test
import oauth_dropins.reddit
from oauth_dropins.webutil.util import json_dumps, json_loads

import models
from . import testutil
from reddit import Reddit


class RedditTest(testutil.AppTest):

  def setUp(self):
    super().setUp()
    oauth_dropins.reddit.REDDIT_APP_KEY = 'my_app_key'
    oauth_dropins.reddit.REDDIT_APP_SECRET = 'my_app_secret'
    user = oauth_dropins.reddit.praw_to_user(gr_reddit_test.FakeRedditor())
    self.auth_entity = oauth_dropins.reddit.RedditAuth(
      id='my_string_id',
      refresh_token='silly_token',
      user_json=json_dumps(user))
    self.auth_entity.put()
    self.r = Reddit.new(auth_entity=self.auth_entity)
    # TODO
    # self.api = self.r.reddit_api = self.mox.CreateMockAnything(praw.Reddit)
    # reddit.user_cache.clear()

  def test_new(self):
    self.assertEqual(self.auth_entity, self.r.auth_entity.get())
    self.assertEqual('silly_token', self.r.gr_source.refresh_token)
    self.assertEqual('bonkerfield', self.r.key.string_id())
    self.assertEqual('https://styles.redditmedia.com/t5_2az095/styles/profileIcon_ek6onop1xbf41.png', self.r.picture)
    self.assertEqual('bonkerfield', self.r.name)
    self.assertEqual('https://reddit.com/user/bonkerfield', self.r.url)
    self.assertEqual('https://reddit.com/user/bonkerfield', self.r.silo_url())
    self.assertEqual('tag:reddit.com,2013:bonkerfield', self.r.user_tag_id())
    self.assertEqual('bonkerfield (Reddit)', self.r.label())

  def test_search_for_links_no_urls(self):
    # only a blocklisted domain
    self.r.domain_urls = ['https://t.co/xyz']
    self.r.put()
    self.assert_equals([], self.r.search_for_links())

  # TODO
  # def test_get_activities_user_id(self):
  #   self.assert_equals([], self.r.get_activities_response())

  # TODO
  # def test_get_activities_converts_404_to_disable_source(self):
  #   self.assert_equals([], self.r.get_activities_response())
