# coding=utf-8
"""Unit tests for wordpress_rest.py.
"""
from __future__ import unicode_literals
from future import standard_library
standard_library.install_aliases()
import urllib.parse

import json
import urllib
import urllib2

import appengine_config
from oauth_dropins.wordpress_rest import WordPressAuth

import testutil
from wordpress_rest import WordPress, AddWordPress


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

  def expect_new_reply(
      self,
      url='https://public-api.wordpress.com/rest/v1/sites/123/posts/456/replies/new?pretty=true',
      content='<a href="http://who">name</a>: foo bar',
      response='{}', status=200, **kwargs):
    self.expect_urlopen(
      url, response, data=urllib.parse.urlencode({'content': content}),
      status=status, **kwargs)
    self.mox.ReplayAll()

  def test_new(self):
    self.expect_urlopen(
      'https://public-api.wordpress.com/rest/v1/sites/123?pretty=true',
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
      'https://public-api.wordpress.com/rest/v1/sites/123?pretty=true',
      json.dumps({'ID': 123, 'URL': 'https://vanity.domain/'}))
    self.mox.ReplayAll()

    w = WordPress.new(self.handler, auth_entity=self.auth_entity)
    self.assertEquals('vanity.domain', w.key.id())
    self.assertEquals('https://vanity.domain/', w.url)
    self.assertEquals(['https://vanity.domain/', 'http://my.wp.com/'],
                      w.domain_urls)
    self.assertEquals(['vanity.domain', 'my.wp.com'], w.domains)

  def test_new_site_domain_same_gr_blog_url(self):
    self.expect_urlopen(
      'https://public-api.wordpress.com/rest/v1/sites/123?pretty=true',
      json.dumps({'ID': 123, 'URL': 'http://my.wp.com/'}))
    self.mox.ReplayAll()

    w = WordPress.new(self.handler, auth_entity=self.auth_entity)
    self.assertEquals(['http://my.wp.com/'], w.domain_urls)
    self.assertEquals(['my.wp.com'], w.domains)

  def test_site_lookup_fails(self):
    self.expect_urlopen(
      'https://public-api.wordpress.com/rest/v1/sites/123?pretty=true',
      'my resp body', status=402)
    self.mox.ReplayAll()
    self.assertRaises(urllib2.HTTPError, WordPress.new, self.handler,
                      auth_entity=self.auth_entity)

  def test_site_lookup_api_disabled_error_start(self):
    self.expect_urlopen(
      'https://public-api.wordpress.com/rest/v1/sites/123?pretty=true',
      '{"error": "unauthorized",'
      ' "message": "API calls to this blog have been disabled."}',
      status=403)
    self.mox.ReplayAll()

    self.assertIsNone(WordPress.new(self.handler, auth_entity=self.auth_entity))
    self.assertIsNone(WordPress.query().get())
    self.assertIn('enable the Jetpack JSON API', next(iter(self.handler.messages)))

  def test_site_lookup_api_disabled_error_finish(self):
    self.expect_urlopen(
      'https://public-api.wordpress.com/rest/v1/sites/123?pretty=true',
      '{"error": "unauthorized",'
      ' "message": "API calls to this blog have been disabled."}',
      status=403)
    self.mox.ReplayAll()

    handler = AddWordPress(self.request, self.response)
    handler.finish(self.auth_entity)
    self.assertIsNone(WordPress.query().get())
    self.assertIn('enable the Jetpack JSON API', next(iter(handler.messages)))

  def test_create_comment_with_slug_lookup(self):
    self.expect_urlopen(
      'https://public-api.wordpress.com/rest/v1/sites/123/posts/'
        'slug:the-slug?pretty=true',
      json.dumps({'ID': 456}))
    self.expect_new_reply(response=json.dumps({'ID': 789, 'ok': 'sgtm'}))

    resp = self.wp.create_comment('http://primary/post/123999/the-slug?asdf',
                                  'name', 'http://who', 'foo bar')
    # ID field gets converted to lower case id
    self.assertEquals({'id': 789, 'ok': 'sgtm'}, resp)

  def test_create_comment_with_unicode_chars(self):
    self.expect_new_reply(content='<a href="http://who">Degenève</a>: foo Degenève bar')

    resp = self.wp.create_comment('http://primary/post/456', 'Degenève',
                                  'http://who', 'foo Degenève bar')
    self.assertEquals({'id': None}, resp)

  def test_create_comment_with_unicode_chars_in_slug(self):
    self.expect_urlopen(
      'https://public-api.wordpress.com/rest/v1/sites/123/posts/slug:✁?pretty=true',
      json.dumps({'ID': 456}))
    self.expect_new_reply()

    resp = self.wp.create_comment('http://primary/post/✁', 'name',
                                  'http://who', 'foo bar')
    self.assertEquals({'id': None}, resp)

  def test_create_comment_gives_up_on_invalid_input_error(self):
    # see https://github.com/snarfed/bridgy/issues/161
    self.expect_new_reply(status=400,
                          response=json.dumps({'error': 'invalid_input'}))

    resp = self.wp.create_comment('http://primary/post/456', 'name',
                                  'http://who', 'foo bar')
    # shouldn't raise an exception
    self.assertEquals({'error': 'invalid_input'}, resp)

  def test_create_comment_gives_up_on_coments_closed(self):
    resp = {'error': 'unauthorized',
            'message': 'Comments on this post are closed'}
    self.expect_new_reply(status=403, response=json.dumps(resp))

    # shouldn't raise an exception
    got = self.wp.create_comment('http://primary/post/456', 'name',
                                 'http://who', 'foo bar')
    self.assertEquals(resp, got)

  def test_create_comment_returns_non_json(self):
    self.expect_new_reply(status=403, response='Forbidden')

    self.assertRaises(urllib2.HTTPError, self.wp.create_comment,
                      'http://primary/post/456', 'name', 'http://who', 'foo bar')
