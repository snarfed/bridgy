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
import twitter_search
from twitter_search import TwitterReply, TwitterSearch

from google.appengine.api import urlfetch
import webapp2


# TODO: uncomment if/when we start using twitter_search again
class TwitterSearchTest(object): #testutil.ModelsTest):

  def setUp(self):
    super(TwitterSearchTest, self).setUp()

    twitter_search.HARD_CODED_DEST = 'FakeDestination'
    self.user = models.User.get_or_insert_current_user(self.handler)
    self.handler.messages = []

    self.search = TwitterSearch(key_name='http://dest1/',
                                owner=self.user,
                                url='http://dest1/',
                                )

    # based on:
    # https://dev.twitter.com/docs/api/1.1/get/search/tweets
    self.tweets = [
      # two embedded urls, only one with expanded_url, no replies
      {'created_at': 'Wed Jan 04 20:10:28 2012 +0000',
       'entities': {'urls': [{'display_url': 'bar.org/qwert',
                              'expanded_url': 'http://bar.org/qwert',
                              'url': 'http://t.co/ZhhEkuxo'},
                             {'display_url': 'bit.ly/dest1_asdf',
                              'url': 'http://bit.ly/dest1_asdf'},
                             ]},
       'user': {'screen_name': 'user1', 'name': 'user 1 name'},
       'id': 1,
       'text': 'this is a tweet',
       },

      # no embedded urls
      {'created_at': 'Tue Jan 03 16:17:16 2012 +0000',
       'user': {'screen_name': 'user2', 'name': 'user 2 name'},
       'id': 2,
       'text': 'this is also a tweet',
       },

      # one embedded url, one reply (below)
      {'created_at': 'Wed Jan 04 09:10:28 2012 +0000',
       'entities': {'urls': [{'display_url': 'dest1/xyz',
                              'expanded_url': 'http://dest1/xyz',
                              'url': 'http://t.co/AhhEkuxo'},
                             ]},
       'user': {'screen_name': 'user3', 'name': 'user 3 name'},
       'id': 3,
       'text': 'this is the last tweet',
       },
      ]
    self.url_search_results = {'statuses': copy.deepcopy(self.tweets)}

    self.tweets_and_urls = []
    for i, link in (0, 'http://dest1/asdf'), (2, 'http://dest1/xyz'):
      self.tweets[i]['bridgy_link'] = link
      self.tweets_and_urls.append((self.tweets[i], link))

    # key is the user id. based on:
    # https://dev.twitter.com/docs/api/1.1/get/search/tweets
    # elements are (user id, search results)
    self.mention_search_results = [
      (1, {'statuses': []}),
      (3, {'statuses': [
           # not a reply
           {'created_at': 'Sun Jan 01 11:44:57 2012 +0000',
            'entities': {'user_mentions': [{'id': 3, 'screen_name': 'user3'}]},
            'user': {'screen_name': 'user4', 'name': 'user 4 name'},
            'id': 4,
            'text': 'boring',
            },
           # reply to tweet id 3 (above)
           {'created_at': 'Sun Jan 01 11:44:57 2012 +0000',
            'entities': {'user_mentions': [{'id': 3, 'screen_name': 'user3'}]},
            'user': {'screen_name': 'user5', 'name': 'user 5 name'},
            'id': 5,
            'in_reply_to_status_id': 3,
            # note the @ mention and hashtag for testing TwitterSearch.linkify()
            'text': '@user3 i hereby #reply',
            'to_user': 'user3',
            },
           ]}),
      (5, {'statuses': [
             # reply to reply tweet id 5 (above)
            {'created_at': 'Sun Jan 01 11:44:57 2013 +0000',
             'entities': {'user_mentions': [{'id': 5, 'screen_name': 'user5'}]},
             'user': {'screen_name': 'user6', 'name': 'user 6 name'},
             'id': 6,
             'in_reply_to_status_id': 5,
             'text': 'we must go deeper',
             'to_user': 'user5',
             },
            ]}),
      (6, {'statuses': [
            # not a reply to anything
            {'user': {'screen_name': 'user2', 'name': 'user 2 name'},
             'id': 999,
             'text': 'too deep!',
             'to_user': 'user6',
             },
            ]}),
      ]

    # TODO: unify with ModelsTest.setUp()
    self.replies = [
      TwitterReply(
        key_name='1',
        created=datetime.datetime(2012, 1, 4, 20, 10, 28),
        source=self.search,
        dest=self.dests[1],
        source_post_url='http://twitter.com/user1/status/1',
        dest_post_url='http://dest1/asdf',
        author_name='user 1 name',
        author_url='http://twitter.com/user1',
        content='this is a tweet',
        username='user1',
        ),
      TwitterReply(
        key_name='3',
        created=datetime.datetime(2012, 1, 4, 9, 10, 28),
        source=self.search,
        dest=self.dests[1],
        source_post_url='http://twitter.com/user3/status/3',
        dest_post_url='http://dest1/xyz',
        author_name='user 3 name',
        author_url='http://twitter.com/user3',
        content='this is the last tweet',
        username='user3',
        ),
      TwitterReply(
        key_name='5',
        created=datetime.datetime(2012, 1, 1, 11, 44, 57),
        source=self.search,
        dest=self.dests[1],
        source_post_url='http://twitter.com/user5/status/5',
        dest_post_url='http://dest1/xyz',
        author_name='user 5 name',
        author_url='http://twitter.com/user5',
        content='<a href="http://twitter.com/user3">@user3</a> i hereby <a href="http://twitter.com/search?q=%23reply">#reply</a>',
        username='user5',
        ),
      TwitterReply(
        key_name='6',
        created=datetime.datetime(2013, 1, 1, 11, 44, 57),
        source=self.search,
        dest=self.dests[1],
        source_post_url='http://twitter.com/user6/status/6',
        dest_post_url='http://dest1/xyz',
        author_name='user 6 name',
        author_url='http://twitter.com/user6',
        content='we must go deeper',
        username='user6',
        ),
      ]

  def test_new(self):
    self.environ['QUERY_STRING'] = urllib.urlencode(
      {'url': 'http://dest1/'})
    self.handler.request = webapp2.Request(self.environ)

    self.assert_entities_equal(
      self.search,
      TwitterSearch.new(self.handler),
      ignore=['created'])

  def test_get_posts_and_get_comments(self):
    self.expect_urlopen('.*/search/tweets\.json\?q=dest1\+filter%3Alinks&.*',
                         json.dumps(self.url_search_results),
                         headers=mox.IgnoreArg())

    # following possibly shortened URLs. errors should be ignored.
    self.expect_urlopen('http://bar.org/qwert', '', follow_redirects=True,
                         method='HEAD').AndRaise(urlfetch.DownloadError())
    self.expect_urlopen(
      'http://bit.ly/dest1_asdf',
      testutil.UrlfetchResult(200, '', final_url='http://dest1/asdf'),
      follow_redirects=True, method='HEAD')

    # mentions
    for user_id, results in self.mention_search_results:
        self.expect_urlopen(
          '.*/search/tweets\.json\?q=%%40user%d\&.*' % user_id,
          json.dumps(results),
          headers=mox.IgnoreArg())
    self.mox.ReplayAll()

    self.assertEqual(self.tweets_and_urls, self.search.get_posts())
    self.assert_entities_equal(
      self.replies,
      self.search.get_comments([(self.tweets[0], self.dests[1]),
                                (self.tweets[2], self.dests[1])]))
