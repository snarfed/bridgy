# coding=utf-8
"""Unit tests for tumblr.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import json
import mox

import appengine_config
from appengine_config import HTTP_TIMEOUT
from models import BlogPost

from activitystreams.oauth_dropins.tumblr import TumblrAuth
import tumblr
from tumblr import Tumblr
import testutil


class TumblrTest(testutil.HandlerTest):

  def setUp(self):
    super(TumblrTest, self).setUp()
    self.auth_entity = TumblrAuth(id='name', user_json=json.dumps({
          'user': {'blogs': [{'url': 'other'},
                             {'url': 'http://primary/', 'primary': True}]}}))
    self.tumblr = Tumblr(disqus_shortname='my-disqus-name')

    appengine_config.DISQUS_API_KEY = 'my key'
    appengine_config.DISQUS_API_SECRET = 'my secret'
    appengine_config.DISQUS_ACCESS_TOKEN = 'my token'

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
      json.dumps(resp),
      params=self.disqus_params({'forum': 'my-disqus-name',
                                 'thread':'link:http://primary/post/123999'}),
      **kwargs)

  def test_new(self):
    t = Tumblr.new(self.handler, auth_entity=self.auth_entity)
    self.assertEquals(self.auth_entity.key, t.auth_entity)
    self.assertEquals('name', t.name)
    self.assertEquals('http://primary/', t.domain_url)
    self.assertEquals('primary', t.domain)
    self.assertEquals('http://api.tumblr.com/v2/blog/primary/avatar/512', t.picture)

  def test_new_no_primary_blog(self):
    self.auth_entity.user_json = json.dumps({'user': {'blogs': [{'url': 'foo'}]}})
    self.assertIsNone(Tumblr.new(self.handler, auth_entity=self.auth_entity))
    self.assertIn('Tumblr blog not found', next(iter(self.handler.messages)))

  def test_new_with_blog_name(self):
    self.auth_entity.user_json = json.dumps({
        'user': {'blogs': [{'url': 'foo'},
                           {'name': 'bar', 'url': 'baz'},
                           {'name': 'biff', 'url': 'http://boff/'},
                           ]}})
    got = Tumblr.new(self.handler, auth_entity=self.auth_entity, blog_name='biff')
    self.assertEquals('http://boff/', got.domain_url)
    self.assertEquals('boff', got.domain)

  def test_verify(self):
    # based on http://snarfed.tumblr.com/
    # this requests.get is called by webmention-tools
    self.expect_requests_get('http://primary/', """
<html><body>
some stuff
<script charset="utf-8" type="text/javascript" src="http://disqus.com/forums/my-disqus-name/get_num_replies.js?url131=...&amp;"></script>
</body></html>""", verify=False)
    self.mox.ReplayAll()

    t = Tumblr.new(self.handler, auth_entity=self.auth_entity)
    t.verify()
    self.assertEquals('my-disqus-name', t.disqus_shortname)

  def test_verify_without_disqus(self):
    self.expect_requests_get('http://primary/', 'no disqus here!', verify=False)
    self.mox.ReplayAll()

    t = Tumblr.new(self.handler, auth_entity=self.auth_entity)
    t.verify()
    self.assertIsNone(t.disqus_shortname)

  def test_create_comment(self):
    self.expect_thread_details()
    self.expect_requests_post(
      tumblr.DISQUS_API_CREATE_POST_URL,
      json.dumps({'response': {'ok': 'sgtm'}}),
      params=self.disqus_params({
            'thread': '87654',
            'message': '<a href="http://who">who</a>: foo bar'}))
    self.mox.ReplayAll()

    resp = self.tumblr.create_comment('http://primary/post/123999/xyz_abc?asdf',
                                      'who', 'http://who', 'foo bar')
    self.assertEquals({'ok': 'sgtm'}, resp)

  def test_create_comment_with_unicode_chars(self):
    self.expect_thread_details()
    self.expect_requests_post(
      tumblr.DISQUS_API_CREATE_POST_URL,
      json.dumps({}),
      params=self.disqus_params({
            'thread': '87654',
            'message': '<a href="http://who">Degenève</a>: foo Degenève bar'}))
    self.mox.ReplayAll()

    resp = self.tumblr.create_comment('http://primary/post/123999/xyz_abc',
                                      u'Degenève', 'http://who', u'foo Degenève bar')

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

  def test_superfeedr_notify(self):
    """Smoke test. Just check that we make it all the way through."""
    Tumblr.new(self.handler, auth_entity=self.auth_entity).put()
    resp = tumblr.application.get_response(
      '/tumblr/notify/primary', method='POST', body=json.dumps({'items': []}))
    self.assertEquals(200, resp.status_int)
