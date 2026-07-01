"""Unit tests for wordpress_rest.py."""
import io
import urllib.request, urllib.parse, urllib.error

from flask import get_flashed_messages
from webutil.testutil import UrlopenResult
from webutil.util import json_dumps, json_loads
from oauth_dropins.wordpress_rest import WordPressAuth
from werkzeug.exceptions import Unauthorized
from werkzeug.routing import RequestRedirect

from flask_app import app
from . import testutil
from wordpress_rest import WordPress, Add


class WordPressTest(testutil.AppTest):

  def setUp(self):
    super().setUp()
    self.auth_entity = WordPressAuth(id='my.wp.com',
                                     user_json=json_dumps({
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

  def urlopen_result(self, response, status=200):
    if status >= 400:
      return urllib.error.HTTPError(
        None, status, 'error', {}, io.BytesIO(response.encode()))
    return UrlopenResult(status, response)

  def expect_new_reply(
      self,
      url='https://public-api.wordpress.com/rest/v1/sites/123/posts/456/replies/new?pretty=true',
      content='<a href="http://who">name</a>: foo bar',
      response='{}', status=200):
    result = self.urlopen_result(response, status)
    if isinstance(result, Exception):
      self.mock_urlopen.side_effect = result
    else:
      self.mock_urlopen.return_value = result

  def test_new(self):
    self.mock_urlopen.return_value = UrlopenResult(200, json_dumps({}))

    w = WordPress.new(auth_entity=self.auth_entity)
    self.assertEqual(self.auth_entity.key, w.auth_entity)
    self.assertEqual('my.wp.com', w.key.id())
    self.assertEqual('Ryan', w.name)
    self.assertEqual(['http://my.wp.com/'], w.domain_urls)
    self.assertEqual(['my.wp.com'], w.domains)
    self.assertEqual('http://ava/tar', w.picture)
    self.assert_urlopen(
      'https://public-api.wordpress.com/rest/v1/sites/123?pretty=true')

  def test_new_with_site_domain(self):
    self.mock_urlopen.return_value = UrlopenResult(
      200, json_dumps({'ID': 123, 'URL': 'https://vanity.domain/'}))

    w = WordPress.new(auth_entity=self.auth_entity)
    self.assertEqual('vanity.domain', w.key.id())
    self.assertEqual('https://vanity.domain/', w.url)
    self.assertEqual(['https://vanity.domain/', 'http://my.wp.com/'],
                      w.domain_urls)
    self.assertEqual(['vanity.domain', 'my.wp.com'], w.domains)
    self.assert_urlopen(
      'https://public-api.wordpress.com/rest/v1/sites/123?pretty=true')

  def test_new_site_domain_same_gr_blog_url(self):
    self.mock_urlopen.return_value = UrlopenResult(
      200, json_dumps({'ID': 123, 'URL': 'http://my.wp.com/'}))

    w = WordPress.new(auth_entity=self.auth_entity)
    self.assertEqual(['http://my.wp.com/'], w.domain_urls)
    self.assertEqual(['my.wp.com'], w.domains)
    self.assert_urlopen(
      'https://public-api.wordpress.com/rest/v1/sites/123?pretty=true')

  def test_site_lookup_fails(self):
    self.mock_urlopen.side_effect = urllib.error.HTTPError(
      None, 402, 'error', {}, io.BytesIO(b'my resp body'))

    with self.assertRaises(urllib.error.HTTPError):
      WordPress.new(auth_entity=self.auth_entity)

  def test_site_lookup_api_disabled_error_start(self):
    self.mock_urlopen.side_effect = urllib.error.HTTPError(
      None, 403, 'error', {},
      io.BytesIO(b'{"error": "unauthorized", "message": "API calls to this blog have been disabled."}'))

    with app.test_request_context():
      with self.assertRaises(RequestRedirect):
        self.assertIsNone(WordPress.new(auth_entity=self.auth_entity))
      self.assertIsNone(WordPress.query().get())
      self.assertIn('enable the Jetpack JSON API', get_flashed_messages()[0])

  def test_site_lookup_api_disabled_error_finish(self):
    self.mock_urlopen.side_effect = urllib.error.HTTPError(
      None, 403, 'error', {},
      io.BytesIO(b'{"error": "unauthorized", "message": "API calls to this blog have been disabled."}'))

    with app.test_request_context():
      with self.assertRaises(RequestRedirect):
        Add('test_site_lookup_api_disabled_error_finish').finish(self.auth_entity)
      self.assertIsNone(WordPress.query().get())
      self.assertIn('enable the Jetpack JSON API', get_flashed_messages()[0])

  def test_create_comment_with_slug_lookup(self):
    self.mock_urlopen.side_effect = [
      UrlopenResult(200, json_dumps({'ID': 456})),
      UrlopenResult(200, json_dumps({'ID': 789, 'ok': 'sgtm'})),
    ]

    resp = self.wp.create_comment('http://primary/post/123999/the-slug?asdf',
                                  'name', 'http://who', 'foo bar')
    # ID field gets converted to lower case id
    self.assertEqual({'id': 789, 'ok': 'sgtm'}, resp)
    self.assert_urlopen(
      'https://public-api.wordpress.com/rest/v1/sites/123/posts/slug:the-slug?pretty=true')
    self.assert_urlopen(
      'https://public-api.wordpress.com/rest/v1/sites/123/posts/456/replies/new?pretty=true')

  def test_create_comment_with_unicode_chars(self):
    self.expect_new_reply(content='<a href="http://who">Degenève</a>: foo Degenève bar')

    resp = self.wp.create_comment('http://primary/post/456', 'Degenève',
                                  'http://who', 'foo Degenève bar')
    self.assertEqual({'id': None}, resp)
    self.assert_urlopen(
      'https://public-api.wordpress.com/rest/v1/sites/123/posts/456/replies/new?pretty=true',
      data=urllib.parse.urlencode(
        {'content': '<a href="http://who">Degenève</a>: foo Degenève bar'}))

  def test_create_comment_with_unicode_chars_in_slug(self):
    self.mock_urlopen.side_effect = [
      UrlopenResult(200, json_dumps({'ID': 456})),
      UrlopenResult(200, '{}'),
    ]

    resp = self.wp.create_comment('http://primary/post/✁', 'name',
                                  'http://who', 'foo bar')
    self.assertEqual({'id': None}, resp)
    self.assert_urlopen(
      'https://public-api.wordpress.com/rest/v1/sites/123/posts/slug:✁?pretty=true')
    self.assert_urlopen(
      'https://public-api.wordpress.com/rest/v1/sites/123/posts/456/replies/new?pretty=true')

  def test_create_comment_gives_up_on_invalid_input_error(self):
    # see https://github.com/snarfed/bridgy/issues/161
    self.expect_new_reply(status=400,
                          response=json_dumps({'error': 'invalid_input'}))

    resp = self.wp.create_comment('http://primary/post/456', 'name',
                                  'http://who', 'foo bar')
    # shouldn't raise an exception
    self.assertEqual({'error': 'invalid_input'}, resp)
    self.assert_urlopen(
      'https://public-api.wordpress.com/rest/v1/sites/123/posts/456/replies/new?pretty=true',
      data=urllib.parse.urlencode({'content': '<a href="http://who">name</a>: foo bar'}))

  def test_create_comment_gives_up_on_coments_closed(self):
    resp = {'error': 'unauthorized',
            'message': 'Comments on this post are closed'}
    self.expect_new_reply(status=403, response=json_dumps(resp))

    # shouldn't raise an exception
    got = self.wp.create_comment('http://primary/post/456', 'name',
                                 'http://who', 'foo bar')
    self.assertEqual(resp, got)
    self.assert_urlopen(
      'https://public-api.wordpress.com/rest/v1/sites/123/posts/456/replies/new?pretty=true')

  def test_create_comment_returns_non_json(self):
    self.expect_new_reply(status=403, response='Forbidden')

    self.assertRaises(urllib.error.HTTPError, self.wp.create_comment,
                      'http://primary/post/456', 'name', 'http://who', 'foo bar')
    self.assert_urlopen(
      'https://public-api.wordpress.com/rest/v1/sites/123/posts/456/replies/new?pretty=true')

  def test_create_comment_user_account_closed(self):
    self.expect_new_reply(status=400, response=json_dumps({
      'error': 'invalid_token',
      'message': 'The user account has been closed.',
    }))

    self.assertRaises(Unauthorized, self.wp.create_comment,
                      'http://primary/post/456', 'name', 'http://who', 'foo bar')
    self.assert_urlopen(
      'https://public-api.wordpress.com/rest/v1/sites/123/posts/456/replies/new?pretty=true')
