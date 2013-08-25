#!/usr/bin/python
"""Unit tests for facebook.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import json
import logging
import mox
import testutil
import urllib
import urlparse

import facebook
from facebook import FacebookApp, FacebookComment, FacebookPage
import models

from google.appengine.ext import webapp


class FacebookTestBase(testutil.ModelsTest):

  def setUp(self):
    super(FacebookTestBase, self).setUp()
    FacebookApp(app_id='app_id', app_secret='app_secret').save()
    self.app = FacebookApp.get()

  def expect_fql(self, query_snippet, results):
    """Stubs out and expects an FQL query via urlfetch.

    Expects my_access_token to be used as the access token.

    Args:
      query_snippet: an unescaped snippet that should be in the query
      results: list or dict of results to return
    """
    comparator = mox.Regex(
      '.*/method/fql.query\?&access_token=my_access_token&format=json&query=(.*)$')

    if query_snippet:
      quoted = urllib.quote(query_snippet)
      comparator = mox.And(comparator, mox.StrContains(quoted))

    self.expect_urlfetch(comparator, json.dumps(results))


class FacebookAppTest(FacebookTestBase):

  def test_get_access_token(self):
    self.app.get_access_token(self.handler, '/redirect_to')
    self.assertEqual(302, self.handler.response.status)
    redirect = self.handler.response.headers['Location']

    parsed = urlparse.urlparse(redirect)
    self.assertEqual('/dialog/oauth/', parsed.path)

    expected_params = {
      'scope': ['read_stream,offline_access'],
      'client_id': ['app_id'],
      'redirect_uri': ['http://HOST/facebook/got_auth_code'],
      'response_type': ['code'],
      'state': ['http://HOST/redirect_to'],
      }
    self.assertEqual(expected_params, urlparse.parse_qs(parsed.query))

  def test_got_auth_code(self):
    comparator = mox.Regex('.*/oauth/access_token\?.*&code=my_auth_code.*')
    self.expect_urlfetch(comparator, 'foo=bar&access_token=my_access_token')

    self.mox.ReplayAll()
    resp = self.get(
      facebook.application,
      '/facebook/got_auth_code',
      302,
      query_params={'code': 'my_auth_code', 'state': 'http://my/redirect_to'})
    self.assertEqual('http://my/redirect_to?access_token=my_access_token',
                     resp.headers['Location'])

  def test_fql(self):
    self.expect_fql('my_query', {'my_key': [ 'my_list']})
    self.mox.ReplayAll()
    self.assertEqual({'my_key': ['my_list']},
                     self.app.fql('my_query', 'my_access_token'))


class FacebookPageTest(FacebookTestBase):

  def setUp(self):
    super(FacebookPageTest, self).setUp()
    facebook.HARD_CODED_DEST = 'FakeDestination'
    self.user = models.User.get_or_insert_current_user(self.handler)
    self.handler.messages = []
    self.page = FacebookPage(key_name='2468',
                             owner=self.user,
                             name='my full name',
                             url='http://my.fb/url',
                             picture='http://my.pic/small',
                             type='user',
                             username='my_username',
                             access_token='my_access_token',
                             )


    # TODO: unify with ModelsTest.setUp()
    self.comments = [
      FacebookComment(
        key_name='123',
        created=datetime.datetime.utcfromtimestamp(1),
        source=self.page,
        dest=self.dests[1],
        source_post_url='https://www.facebook.com/permalink.php?story_fbid=1&id=4',
        dest_post_url='http://dest1/post/url',
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
        dest=self.dests[0],
        source_post_url='https://www.facebook.com/permalink.php?story_fbid=2&id=5',
        dest_post_url='http://dest0/post/url',
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
        dest=self.dests[1],
        source_post_url='https://www.facebook.com/permalink.php?story_fbid=1&id=6',
        dest_post_url='http://dest1/post/url',
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

  def test_new(self):
    self.expect_fql('FROM profile WHERE id = me()', self.new_fql_results)
    self.mox.ReplayAll()

    self.environ['QUERY_STRING'] = urllib.urlencode(
      {'access_token': 'my_access_token'})
    self.handler.request = webapp.Request(self.environ)
    self.assert_entities_equal(self.page,
                               FacebookPage.new(self.handler),
                               ignore=['created'])

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
        {'link_id': 1, 'url': 'http://dest1/post/url'},
        {'link_id': 2, 'url': 'http://dest0/post/url'},
        ])
    self.expect_fql('SELECT id, name, url FROM profile ', [
        {'id': 4, 'name': 'fred', 'url': 'http://fred'},
        {'id': 5, 'name': 'bob', 'url': 'http://bob'},
        {'id': 6, 'name': 'alice', 'url': 'http://alice'},
        ])
    self.mox.ReplayAll()

    self.assertEqual(
      [(1, 'http://dest1/post/url'), (2, 'http://dest0/post/url')],
      self.page.get_posts())

    self.assert_entities_equal(
      self.comments,
      self.page.get_comments([(1, self.dests[1]), (2, self.dests[0])]))

  def test_disable_on_auth_failure(self):
    self.expect_urlfetch(
      '.*',
      json.dumps({
          'error_code': 190,
          'error_msg': 'Error validating access token: User 12345 has not authorized application 67890.',
          'request_args': [{}]}))
    self.mox.ReplayAll()

    self.assertRaises(models.Deauthorized, self.page.get_posts)
