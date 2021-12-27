# coding=utf-8
"""Unit tests for tumblr.py.
"""
from flask import get_flashed_messages
from mox3 import mox
from oauth_dropins.tumblr import TumblrAuth
from oauth_dropins.webutil.util import json_dumps, json_loads
from werkzeug.exceptions import BadRequest

from flask_app import app
import tumblr
from tumblr import Tumblr
from . import testutil


class TumblrTest(testutil.AppTest):

  def setUp(self):
    super().setUp()
    self.auth_entity = TumblrAuth(id='name', user_json=json_dumps({
          'user': {'blogs': [{'url': 'other'},
                             {'url': 'http://primary/', 'primary': True}]}}))
    self.tumblr = Tumblr(id='my id', disqus_shortname='my-disqus-name')

    tumblr.DISQUS_API_KEY = 'my key'
    tumblr.DISQUS_API_SECRET = 'my secret'
    tumblr.DISQUS_ACCESS_TOKEN = 'my token'

  def disqus_params(self, params):
    params.update({
        'api_key': 'my key',
        'api_secret': 'my secret',
        'access_token': 'my token',
        })
    return params

  def expect_thread_details(self, resp=None, **kwargs):
    if resp is None:
      resp = {'response': {'id': '87654'}}
    self.expect_requests_get(
      tumblr.DISQUS_API_THREAD_DETAILS_URL,
      json_dumps(resp),
      params=self.disqus_params({'forum': 'my-disqus-name',
                                 'thread':'link:http://primary/post/123999'}),
      **kwargs)

  def test_new(self):
    t = Tumblr.new(auth_entity=self.auth_entity)
    self.assertEqual(self.auth_entity.key, t.auth_entity)
    self.assertEqual('name', t.name)
    self.assertEqual(['http://primary/'], t.domain_urls)
    self.assertEqual(['primary'], t.domains)
    self.assertEqual('http://api.tumblr.com/v2/blog/primary/avatar/512', t.picture)

  def test_new_no_primary_blog(self):
    self.auth_entity.user_json = json_dumps({'user': {'blogs': [{'url': 'foo'}]}})
    with app.test_request_context():
      self.assertIsNone(Tumblr.new(auth_entity=self.auth_entity))
      self.assertIn('Tumblr blog not found', get_flashed_messages()[0])

  def test_new_with_blog_name(self):
    self.auth_entity.user_json = json_dumps({
        'user': {'blogs': [{'url': 'foo'},
                           {'name': 'bar', 'url': 'baz'},
                           {'name': 'biff', 'url': 'http://boff/'},
                           ]}})
    got = Tumblr.new(auth_entity=self.auth_entity, blog_name='biff')
    self.assertEqual(['http://boff/'], got.domain_urls)
    self.assertEqual(['boff'], got.domains)

  def test_verify_default(self):
    # based on http://snarfed.tumblr.com/
    self._test_verify_finds_disqus('<script src="http://disqus.com/forums/my-disqus-name/get_num_replies.js?url131=...&amp;"></script>')

  def test_verify_inspirewell_theme_1(self):
    # based on http://circusriot.tumblr.com/
    self._test_verify_finds_disqus("  var disqus_shortname = 'my-disqus-name';")

  def test_verify_inspirewell_theme_2(self):
    # based on http://circusriot.tumblr.com/
    self._test_verify_finds_disqus('  disqusUsername = "my-disqus-name";')

  def test_verify_require_aorcsik_theme(self):
    # based on http://require.aorcsik.com/
    self._test_verify_finds_disqus(
      '  dsq.src = "http://my-disqus-name.disqus.com/embed.js";')

  def _test_verify_finds_disqus(self, snippet):
    self.expect_requests_get(
      'http://primary/', f'<html>\nstuff\n{snippet}\n</html>')
    self.mox.ReplayAll()
    t = Tumblr.new(auth_entity=self.auth_entity, features=['webmention'])
    t.verify()
    self.assertEqual('my-disqus-name', t.disqus_shortname)

  def test_verify_without_disqus(self):
    self.expect_requests_get('http://primary/', 'no disqus here!')
    self.mox.ReplayAll()
    t = Tumblr.new(auth_entity=self.auth_entity, features=['webmention'])
    t.verify()
    self.assertIsNone(t.disqus_shortname)

  def test_create_comment(self):
    self.expect_thread_details()
    self.expect_requests_post(
      tumblr.DISQUS_API_CREATE_POST_URL,
      json_dumps({'response': {'ok': 'sgtm'}}),
      params=self.disqus_params({
            'thread': '87654',
            'message': '<a href="http://who">who</a>: foo bar'}))
    self.mox.ReplayAll()

    resp = self.tumblr.create_comment('http://primary/post/123999/xyz_abc?asdf',
                                      'who', 'http://who', 'foo bar')
    self.assertEqual({'ok': 'sgtm'}, resp)

  def test_create_comment_with_unicode_chars(self):
    self.expect_thread_details()
    self.expect_requests_post(
      tumblr.DISQUS_API_CREATE_POST_URL,
      json_dumps({}),
      params=self.disqus_params({
        'thread': '87654',
        'message': '<a href="http://who">Degenève</a>: foo Degenève bar',
      }))
    self.mox.ReplayAll()

    resp = self.tumblr.create_comment('http://primary/post/123999/xyz_abc',
                                      'Degenève', 'http://who', 'foo Degenève bar')
    self.assertEqual({}, resp)

  def test_create_comment_finds_disqus_shortname(self):
    self.tumblr.disqus_shortname = None

    self.expect_requests_get('http://primary/post/123999',
                             "fooo var disqus_shortname = 'my-disqus-name';")
    self.expect_thread_details()
    self.expect_requests_post(tumblr.DISQUS_API_CREATE_POST_URL,
                              json_dumps({}), params=mox.IgnoreArg())
    self.mox.ReplayAll()

    self.tumblr.create_comment('http://primary/post/123999', '', '', '')
    self.assertEqual('my-disqus-name', self.tumblr.key.get().disqus_shortname)

  def test_create_comment_doesnt_find_disqus_shortname(self):
    self.tumblr.disqus_shortname = None

    self.expect_requests_get('http://primary/post/123999', 'no shortname here')
    self.mox.ReplayAll()

    with self.assertRaises(BadRequest):
      self.tumblr.create_comment('http://primary/post/123999', '', '', '')

  # not implemented yet. see https://github.com/snarfed/bridgy/issues/177.
  # currently handled in webmention.error().
  # def test_create_comment_thread_lookup_fails(self):
  #   error = {
  #     'code':2,
  #     'response': "Invalid argument, 'thread': Unable to find thread 'link:xyz'",
  #     }
  #   self.expect_thread_details(status_code=400, resp=error)
  #   self.mox.ReplayAll()

  #   resp = self.tumblr.create_comment('http://primary/post/123999/xyz_abc',
  #                                     'who', 'http://who', 'foo bar')
  #   self.assert_equals(error, resp)
