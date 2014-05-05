"""Unit tests for wordpress_rest.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import json
import mox
import urllib

import appengine_config
from appengine_config import HTTP_TIMEOUT
from models import BlogPost

from activitystreams.oauth_dropins.wordpress_rest import WordPressAuth
import wordpress_rest
from wordpress_rest import WordPress
import testutil


class WordPressTest(testutil.HandlerTest):

  def setUp(self):
    super(WordPressTest, self).setUp()
    self.auth_entity = WordPressAuth(id='my.wp.com',
                                     user_json=json.dumps({
                                       'display_name': 'Ryan',
                                       'username': 'ry',
                                       'avatar_URL': 'http://ava/tar'}),
                                     blog_id='123',
                                     blog_url='http://my.wp.com/',
                                     access_token_str='my token')

  def test_new(self):
    w = WordPress.new(self.handler, auth_entity=self.auth_entity)
    self.assertEquals(self.auth_entity.key, w.auth_entity)
    self.assertEquals('my.wp.com', w.key.id())
    self.assertEquals('Ryan', w.name)
    self.assertEquals('http://my.wp.com/', w.domain_url)
    self.assertEquals('my.wp.com', w.domain)
    self.assertEquals('http://ava/tar', w.picture)

  def test_create_comment_with_slug_lookup(self):
    self.expect_urlopen(
      'https://public-api.wordpress.com/rest/v1/sites/my.wp.com/posts/'
      'slug:the-slug?pretty=true',
      json.dumps({'ID': 456}))

    self.expect_urlopen(
      'https://public-api.wordpress.com/rest/v1/sites/my.wp.com/posts/'
      '456/replies/new?pretty=true',
      json.dumps({'ID': 789, 'ok': 'sgtm'}),
      data=urllib.urlencode({'content': '<a href="http://who">who</a>: foo bar'}))

    self.mox.ReplayAll()

    self.auth_entity.put()
    wp = WordPress.new(self.handler, auth_entity=self.auth_entity)
    resp = wp.create_comment('http://primary/post/123999/the-slug?asdf',
                             'who', 'http://who', 'foo bar')
    self.assertEquals({'id': 789, 'ok': 'sgtm'}, resp)

  def test_superfeedr_notify(self):
    """Smoke test. Just check that we make it all the way through."""
    WordPress.new(self.handler, auth_entity=self.auth_entity).put()
    resp = wordpress_rest.application.get_response(
      '/wordpress/notify/111', method='POST', body=json.dumps({'items': []}))
    self.assertEquals(200, resp.status_int)
