#!/usr/bin/python
"""Unit tests for twitter.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

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
    self.url_search_results = {'results': [
        # two embedded urls, no replies
        {'created_at': 'Wed, 04 Jan 2012 20:10:28 +0000',
         'entities': {'urls': [{'display_url': 'bar.org/qwert',
                                'expanded_url': 'http://bar.org/qwert',
                                'url': 'http://t.co/ZhhEkuxo'},
                               {'display_url': 'dest1/asdf',
                                'expanded_url': 'http://dest1/asdf',
                                'url': 'http://t.co/ghhEkuxo'},
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
        ]}

    # index is the user id. based on:
    # http://search.twitter.com/search.json?q=@snarfed_org+filter:links&include_entities=true
    self.mention_search_results = [
      None,  # no user id 0
      {'results': []},
      {'results': []},
      {'results': [
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
        ]},
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

  def test_poll(self):
    self.expect_urlfetch('.*/search\.json\?q=dest1\+filter%3Alinks&.*',
                         json.dumps(self.url_search_results))
    for i in range(1, 4):
        self.expect_urlfetch(
          '.*/search\.json\?q=%%40user%d\+filter%%3Alinks&.*' % i,
          json.dumps(self.mention_search_results[i]))
    self.mox.ReplayAll()

    got = self.search.poll()
    self.assert_entities_equal(self.replies, got)
