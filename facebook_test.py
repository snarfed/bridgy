#!/usr/bin/python
"""Unit tests for facebook.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import json
import logging
import mox
import re
import testutil
import urllib
import urlparse

from activitystreams.oauth_dropins import facebook as oauth_facebook
import facebook
from facebook import FacebookComment, FacebookPage
import models

import webapp2


class FacebookPageTest(testutil.ModelsTest):

  def setUp(self):
    super(FacebookPageTest, self).setUp()
    self.user = models.User.get_or_insert_current_user(self.handler)
    self.handler.messages = []
    self.auth_entity = oauth_facebook.FacebookAuth(
      key_name='x', auth_code='x', access_token_str='x', user_json='x')
    self.page = FacebookPage(key_name='2468',
                             owner=self.user,
                             name='my full name',
                             url='http://my.fb/url',
                             picture='http://my.pic/small',
                             type='user',
                             username='my_username',
                             auth_entity=self.auth_entity)

    # TODO: unify with ModelsTest.setUp()
    self.comments = [
      FacebookComment(
        key_name='123',
        created=datetime.datetime.utcfromtimestamp(1),
        source=self.page,
        source_post_url='https://www.facebook.com/permalink.php?story_fbid=1&id=4',
        target_post_url='http://target1/post/url',
        author_name='fred',
        author_url='http://fred',
        content='foo',
        fb_fromid=4,
        fb_username='',
        fb_object_id=1,
        ),
      FacebookComment(
        key_name='456',
        created=datetime.datetime.utcfromtimestamp(2),
        source=self.page,
        source_post_url='https://www.facebook.com/permalink.php?story_fbid=2&id=5',
        target_post_url='http://target0/post/url',
        author_name='bob',
        author_url='http://bob',
        content='bar',
        fb_fromid=5,
        fb_username='',
        fb_object_id=2,
        ),
      FacebookComment(
        key_name='789',
        created=datetime.datetime.utcfromtimestamp(3),
        source=self.page,
        source_post_url='https://www.facebook.com/permalink.php?story_fbid=1&id=6',
        target_post_url='http://target1/post/url',
        author_name='alice',
        author_url='http://alice',
        content='baz',
        fb_fromid=6,
        fb_username='',
        fb_object_id=1,
        ),
      ]

    self.sources[0].set_comments(self.comments)

    self.new_fql_results = [{
        'id': '2468',
        'name': 'my full name',
        'url': 'http://my.fb/url',
        'pic_small': 'http://my.pic/small',
        'type': 'user',
        'username': 'my_username',
        }]

  def expect_fql(self, query_snippet='', results=None):
    """Stubs out and expects an FQL query via urlopen.

    Args:
      query_snippet: an unescaped snippet that should be in the query
      results: list or dict of results to return
    """
    self.expect_urlopen(re.compile('.*%s.*' %
                                   re.escape(urllib.quote_plus(query_snippet))),
                        json.dumps(results))

  def test_fql(self):
    self.expect_fql('my_query', {'my_key': [ 'my_list']})
    self.mox.ReplayAll()
    self.assertEqual({'my_key': ['my_list']}, self.page.fql('my_query'))

  def test_get_posts_and_get_comments(self):
    self.expect_fql('SELECT post_fbid, ', [
        {'post_fbid': '123', 'object_id': 1, 'fromid': 4,
         'username': '', 'time': 1, 'text': 'foo'},
        {'post_fbid': '456', 'object_id': 2, 'fromid': 5,
         'username': '', 'time': 2, 'text': 'bar'},
        {'post_fbid': '789', 'object_id': 1, 'fromid': 6,
         'username': '', 'time': 3, 'text': 'baz'},
        ])
    self.expect_fql('SELECT link_id, url FROM link ', [
        {'link_id': 1, 'url': 'http://target1/post/url'},
        {'link_id': 2, 'url': 'http://target0/post/url'},
        ])
    self.expect_fql('SELECT id, name, url FROM profile ', [
        {'id': 4, 'name': 'fred', 'url': 'http://fred'},
        {'id': 5, 'name': 'bob', 'url': 'http://bob'},
        {'id': 6, 'name': 'alice', 'url': 'http://alice'},
        ])
    self.mox.ReplayAll()

    self.assertEqual(
      [(1, 'http://target1/post/url'), (2, 'http://target0/post/url')],
      self.page.get_posts())

    self.assert_entities_equal(
      self.comments,
      self.page.get_comments([(1, 'http://target0/post/url'),
                              (2, 'http://target1/post/url')]))

  def test_disable_on_auth_failure(self):
    self.expect_urlopen(
      re.compile('.*'),
      json.dumps({
          'error_code': 190,
          'error_msg': 'Error validating access token: User 12345 has not authorized application 67890.',
          'request_args': [{}]}))
    self.mox.ReplayAll()

    self.assertRaises(models.DisableSource, self.page.get_posts)

  def test_get_post(self):
    self.expect_urlopen('https://graph.facebook.com/123?access_token=x',
                        json.dumps({'id': '123', 'message': 'asdf'}))
    self.mox.ReplayAll()
    self.assertEquals({'id': 'tag:facebook.com,2013:123',
                       'url': 'http://facebook.com/123',
                       'objectType': 'note',
                       'content': 'asdf'},
                      self.page.get_post('123'))

  def test_get_comment(self):
    self.expect_urlopen('https://graph.facebook.com/456_789?access_token=x',
                        json.dumps({'id': '456_789', 'message': 'qwert'}))
    self.mox.ReplayAll()
    self.assertEquals({'id': 'tag:facebook.com,2013:456_789',
                       'url': 'http://facebook.com/456?comment_id=789',
                       'objectType': 'comment',
                       'content': 'qwert',
                       'inReplyTo': {'id': 'tag:facebook.com,2013:456'}},
                      self.page.get_comment('456_789'))
