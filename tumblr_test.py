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

  def test_new(self):
    # based on http://snarfed.tumblr.com/
    self.expect_requests_get('http://primary/', """
<html><body>
some stuff
<script charset="utf-8" type="text/javascript" src="http://disqus.com/forums/my-disqus-name/get_num_replies.js?url131=...&amp;"></script>
</body></html>""")
    self.mox.ReplayAll()

    t = Tumblr.new(self.handler, auth_entity=self.auth_entity)
    self.assertEquals(self.auth_entity.key, t.auth_entity)
    self.assertEquals('name', t.name)
    self.assertEquals('http://primary/', t.domain_url)
    self.assertEquals('primary', t.domain)
    self.assertEquals('my-disqus-name', t.disqus_shortname)
    self.assertEquals('http://api.tumblr.com/v2/blog/primary/avatar/512', t.picture)

  def test_new_no_primary_blog(self):
    self.auth_entity.user_json = json.dumps({'user': {'blogs': [{'url': 'foo'}]}})
    self.assertIsNone(Tumblr.new(self.handler, auth_entity=self.auth_entity))
    self.assertIn('No primary Tumblr blog', next(iter(self.handler.messages)))

  def test_new_without_disqus(self):
    self.expect_requests_get('http://primary/', 'no disqus here!')
    self.mox.ReplayAll()

    self.assertIsNone(Tumblr.new(self.handler, auth_entity=self.auth_entity))
    self.assertIn('install Disqus', next(iter(self.handler.messages)))

  def test_create_comment(self):
    appengine_config.DISQUS_API_KEY = 'my key'
    appengine_config.DISQUS_API_SECRET = 'my secret'
    appengine_config.DISQUS_ACCESS_TOKEN = 'my token'

    self.expect_requests_get(
      tumblr.DISQUS_API_THREAD_DETAILS_URL,
      json.dumps({'response': {'id': '87654'}}),
      params={'forum': 'my-disqus-name',
              'thread':'link:http://primary/post/123999',
              'api_key': 'my key',
              'api_secret': 'my secret',
              'access_token': 'my token',
              })

    self.expect_requests_post(
      tumblr.DISQUS_API_CREATE_POST_URL,
      json.dumps({'response': {'ok': 'sgtm'}}),
      params={'thread': '87654',
              'message': '<a href="http://who">who</a>: foo bar',
              'api_key': 'my key',
              'api_secret': 'my secret',
              'access_token': 'my token',
              })

    self.mox.ReplayAll()

    t = Tumblr(disqus_shortname='my-disqus-name')
    resp = t.create_comment('http://primary/post/123999/xyz_abc?asdf',
                            'who', 'http://who', 'foo bar')
    self.assertEquals({'ok': 'sgtm'}, resp)

  def test_superfeedr_notify(self):
    """Smoke test. Just check that we make it all the way through."""
    self.expect_requests_get('http://primary/',
                             'http://disqus.com/forums/my-disqus-name/')
    self.mox.ReplayAll()

    Tumblr.new(self.handler, auth_entity=self.auth_entity).put()
    resp = tumblr.application.get_response(
      '/tumblr/notify/primary', method='POST', body=json.dumps({'items': []}))
    self.assertEquals(200, resp.status_int)
