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
from facebook import FacebookPage
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
