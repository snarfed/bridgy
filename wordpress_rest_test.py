# coding=utf-8
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
    self.auth_entity.put()
    self.wp = WordPress(id='my.wp.com',
                        auth_entity=self.auth_entity.key,
                        url='http://my.wp.com/',
                        domains=['my.wp.com'])

  def test_new(self):
    self.expect_urlopen(
      'https://public-api.wordpress.com/rest/v1/sites/my.wp.com?pretty=true',
      json.dumps({}))
    self.mox.ReplayAll()

    w = WordPress.new(self.handler, auth_entity=self.auth_entity)
    self.assertEquals(self.auth_entity.key, w.auth_entity)
    self.assertEquals('my.wp.com', w.key.id())
    self.assertEquals('Ryan', w.name)
    self.assertEquals(['http://my.wp.com/'], w.domain_urls)
    self.assertEquals(['my.wp.com'], w.domains)
    self.assertEquals('http://ava/tar', w.picture)

  def test_new_with_site_domain(self):
    self.expect_urlopen(
      'https://public-api.wordpress.com/rest/v1/sites/my.wp.com?pretty=true',
      json.dumps({'ID': 123, 'URL': 'https://vanity.domain/'}))
    self.mox.ReplayAll()

    w = WordPress.new(self.handler, auth_entity=self.auth_entity)
    self.assertEquals('vanity.domain', w.key.id())
    self.assertEquals('https://vanity.domain/', w.url)
    self.assertEquals(['https://vanity.domain/', 'http://my.wp.com/'],
                      w.domain_urls)
    self.assertEquals(['vanity.domain', 'my.wp.com'], w.domains)

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

    resp = self.wp.create_comment('http://primary/post/123999/the-slug?asdf',
                                  'who', 'http://who', 'foo bar')
    self.assertEquals({'id': 789, 'ok': 'sgtm'}, resp)

  def test_create_comment_with_unicode_chars(self):
    self.expect_urlopen(
      'https://public-api.wordpress.com/rest/v1/sites/my.wp.com/posts/'
      '123/replies/new?pretty=true',
      json.dumps({}),
      data=urllib.urlencode({
          'content': '<a href="http://who">Degenève</a>: foo Degenève bar'}))
    self.mox.ReplayAll()

    resp = self.wp.create_comment('http://primary/post/123', u'Degenève',
                                  'http://who', u'foo Degenève bar')

  def test_create_comment_gives_up_on_invalid_input_error(self):
    # see https://github.com/snarfed/bridgy/issues/161
    self.expect_urlopen(
      'https://public-api.wordpress.com/rest/v1/sites/my.wp.com/posts/'
      '123/replies/new?pretty=true',
      json.dumps({'error': 'invalid_input'}),
      status=400,
      data=urllib.urlencode({'content': '<a href="http://who">name</a>: foo'}))
    self.mox.ReplayAll()

    resp = self.wp.create_comment('http://primary/post/123', 'name',
                                  'http://who', 'foo')
    # shouldn't raise an exception
    self.assertEquals({'error': 'invalid_input'}, resp)

  def test_superfeedr_notify(self):
    """Smoke test. Just check that we make it all the way through."""
    resp = wordpress_rest.application.get_response(
      '/wordpress/notify/111', method='POST', body=json.dumps({'items': []}))
    self.assertEquals(200, resp.status_int)

  def test_preprocess_superfeedr_item(self):
    def test(expected, input):
      item = {'content': input}
      self.wp.preprocess_superfeedr_item(item)
      self.assert_equals(expected, item['content'])

    for unchanged in ('', 'a b c', 'a http://foo b',
                      ' Filed under: foo', ' Tagged under: bar',
                      'stagged: <a href=x">', 'profiled under: <a href=x">'):
      test(unchanged, unchanged)

    for clear in (' Filed under: <a href="foo"></a>',
                  ' Tagged: <a href="bar"></a>'):
      test(' ', clear)

    test('a http://foo ',
         'a http://foo Filed under: <a href="foo"></a> Tagged: <a href="bar"></a>')
