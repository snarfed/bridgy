#!/usr/bin/python
"""Unit tests for twitter.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import copy
import datetime
import json
import mox
import urllib

import models
import testutil
import twitter
from twitter import TwitterReply, TwitterSearch

from google.appengine.ext import webapp


class TwitterSearchTest(testutil.ModelsTest):

  def setUp(self):
    super(TwitterSearchTest, self).setUp()

    twitter.HARD_CODED_DEST = 'FakeDestination'
    self.user = models.User.get_or_insert_current_user(self.handler)
    self.handler.messages = []

    self.search = TwitterSearch(key_name='http://dest1/',
                                owner=self.user,
                                url='http://dest1/',
                                )

    # based on:
    # http://search.twitter.com/search.json?q=snarfed.org+filter:links&include_entities=true
    self.tweets = [
      # two embedded urls, only one with expanded_url, no replies
      {'created_at': 'Wed, 04 Jan 2012 20:10:28 +0000',
       'entities': {'urls': [{'display_url': 'bar.org/qwert',
                              'expanded_url': 'http://bar.org/qwert',
                              'url': 'http://t.co/ZhhEkuxo'},
                             {'display_url': 'dest1/asdf',
                              'url': 'http://dest1/asdf'},
                             ]},
       'from_user': 'user1',
       'from_user_name': 'user 1 name',
       'id': 1,
       'text': 'this is a tweet',
       },

      # no embedded urls
      {'created_at': 'Tue, 03 Jan 2012 16:17:16 +0000',
       'entities': {},
       'from_user': 'user2',
       'from_user_name': 'user 2 name',
       'id': 2,
       'text': 'this is also a tweet',
       },

      # two embedded urls, one reply (below)
      {'created_at': 'Wed, 04 Jan 2012 09:10:28 +0000',
       'entities': {'urls': [{'display_url': 'dest1/xyz',
                              'expanded_url': 'http://dest1/xyz',
                              'url': 'http://t.co/AhhEkuxo'},
                             ]},
       'from_user': 'user3',
       'from_user_name': 'user 3 name',
       'id': 3,
       'text': 'this is the last tweet',
       },
      ]
    self.url_search_results = {'results': copy.deepcopy(self.tweets)}

    self.tweets_and_urls = []
    for i, link in (0, 'http://dest1/asdf'), (2, 'http://dest1/xyz'):
      self.tweets[i]['bridgy_link'] = link
      self.tweets_and_urls.append((self.tweets[i], link))

    # index is the user id. based on:
    # http://search.twitter.com/search.json?q=@snarfed_org+filter:links&include_entities=true
    self.mentions = [
      # not a reply
      {'created_at': 'Sun, 01 Jan 2012 11:44:57 +0000',
       'entities': {'user_mentions': [{'id': 3, 'screen_name': 'user3'}]},
       'from_user': 'user4',
       'from_user_name': 'user 4 name',
       'id': 4,
       'text': 'boring',
       },
      # reply to tweet id 3 (above)
      {'created_at': 'Sun, 01 Jan 1970 00:00:01 +0000',
       'entities': {'user_mentions': [{'id': 3, 'screen_name': 'user3'}]},
       'from_user': 'user5',
       'from_user_name': 'user 5 name',
       'id': 5,
       'in_reply_to_status_id': 3,
       # note the @ mention and hashtag for testing TwitterSearch.linkify()
       'text': '@user3 i hereby #reply',
       'to_user': 'user3',
       },
      ]
    # elements are (user id, search results)
    self.mention_search_results = [
      (1, {'results': []}),
      (3, {'results': self.mentions}),
      ]

    # TODO: unify with ModelsTest.setUp()
    self.replies = [TwitterReply(
        key_name='5',
        created=datetime.datetime.utcfromtimestamp(1),
        source=self.search,
        dest=self.dests[1],
        source_post_url='http://twitter.com/user5/status/5',
        dest_post_url='http://dest1/xyz',
        author_name='user 5 name',
        author_url='http://twitter.com/user5',
        content='<a href="http://twitter.com/user3">@user3</a> i hereby <a href="http://twitter.com/search?q=%23reply">#reply</a>',
        username='user5',
        )]

  def test_new(self):
    self.environ['QUERY_STRING'] = urllib.urlencode(
      {'url': 'http://dest1/'})
    self.handler.request = webapp.Request(self.environ)

    self.assert_entities_equal(
      self.search,
      TwitterSearch.new(self.handler),
      ignore=['created'])

  def test_get_posts_and_get_comments(self):
    self.expect_urlfetch('.*/search\.json\?q=dest1\+filter%3Alinks&.*',
                         json.dumps(self.url_search_results))
    for user_id, results in self.mention_search_results:
        self.expect_urlfetch(
          '.*/search\.json\?q=%%40user%d\+filter%%3Alinks&.*' % user_id,
          json.dumps(results))
    self.mox.ReplayAll()

    self.assertEqual(self.tweets_and_urls, self.search.get_posts())
    self.assert_entities_equal(
      self.replies,
      self.search.get_comments([(self.tweets[0], self.dests[1]),
                                (self.tweets[2], self.dests[1])]))
