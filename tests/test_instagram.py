"""Unit tests for instagram.py.
"""
import copy
from unittest import skip
import urllib.request, urllib.parse, urllib.error

from granary import instagram as gr_instagram
from granary.tests import test_instagram as gr_test_instagram
from oauth_dropins.webutil.testutil import TestCase
from oauth_dropins.webutil.util import json_dumps, json_loads
import requests

import app
import instagram
from .testutil import ModelsTest, instagram_profile_user
import util


PROFILE_USER = copy.deepcopy(
  gr_test_instagram.HTML_PROFILE['entry_data']['ProfilePage'][0]['graphql']['user'])
PROFILE_USER['id'] = '987'


@skip('in progress disabling Instagram temporarily')
class InstagramTest(ModelsTest):

  def setUp(self):
    super(InstagramTest, self).setUp()
    self.handler.messages = []
    self.inst = instagram.Instagram.new(
      self.handler, actor={
        'objectType': 'person',
        'id': 'tag:instagram.com,2013:420973239',
        'username': 'snarfed',
        'displayName': 'Ryan Barrett',
        'url': 'https://snarfed.org/',
        'image': {'url': 'http://pic.ture/url'},
        # ...
      })

  def test_new(self):
    self.assertIsNone(self.inst.auth_entity)
    self.assertEqual('snarfed', self.inst.key.string_id())
    self.assertEqual('http://pic.ture/url', self.inst.picture)
    self.assertEqual('https://www.instagram.com/snarfed/', self.inst.url)
    self.assertEqual('https://www.instagram.com/snarfed/', self.inst.silo_url())
    self.assertEqual('tag:instagram.com,2013:420973239', self.inst.user_tag_id())
    self.assertEqual('Ryan Barrett', self.inst.name)
    self.assertEqual('snarfed (Instagram)', self.inst.label())

  def test_canonicalize_url(self):
    self.unstub_requests_head()
    for url in (
        'http://www.instagram.com/p/abcd',
        'https://www.instagram.com/p/abcd',
        'https://www.instagram.com/p/abcd/',
        'https://instagram.com/p/abcd',
    ):
      self.assertEqual('https://www.instagram.com/p/abcd/',
                       self.inst.canonicalize_url(url))

    self.assertIsNone(self.inst.canonicalize_url('https://www.foo.com/p/abcd/'))

  def test_canonicalize_url_approve_checks_full_url(self):
    """...specifically, that the regex ends with a $
    https://github.com/snarfed/bridgy/issues/686
    """
    self.assertEqual('https://www.instagram.com/p/abcd/123/',
                     self.inst.canonicalize_url('https://www.instagram.com/p/abcd/123'))

  def expect_site_fetch(self, body=None):
    if body is None:
      body = """
<html><body>
<a rel="me" href="https://www.instagram.com/snarfed">me on insta</a>
</body></html>
"""
    return TestCase.expect_requests_get(self, 'http://snarfed.org', body)

  def expect_webmention_discovery(self):
    return self.expect_requests_get('https://snarfed.org', '', stream=None,
                                    verify=False)

  def test_signup_success(self):
    self.expect_site_fetch()
    self.expect_webmention_discovery()

    self.mox.ReplayAll()
    resp = self.callback()
    self.assertEqual('http://localhost/instagram/snarfed', resp.headers['Location'])

  def test_signup_no_rel_me(self):
    self.expect_site_fetch('')

    self.mox.ReplayAll()
    resp = self.callback()
    location = urllib.parse.unquote_plus(resp.headers['Location'])
    self.assertTrue(location.startswith(
      'http://localhost/#!No Instagram profile found.'), location)

  def test_signup_no_instagram_profile(self):
    self.expect_site_fetch()

    self.mox.ReplayAll()
    resp = self.callback()
    location = urllib.parse.unquote_plus(resp.headers['Location'])
    self.assertTrue(location.startswith(
      "http://localhost/#!Couldn't find Instagram user 'snarfed'"), location)

  def test_signup_no_instagram_profile_backlink(self):
    self.expect_site_fetch()

    user = copy.deepcopy(PROFILE_USER)
    del user['external_url']

    self.mox.ReplayAll()
    resp = self.callback()
    location = urllib.parse.unquote_plus(resp.headers['Location'])
    self.assertTrue(location.startswith(
      'http://localhost/#!Please add https://snarfed.org to your Instagram'), location)

  def test_signup_private_account(self):
    self.expect_site_fetch()

    user = copy.deepcopy(PROFILE_USER)
    user['is_private'] = True

    self.mox.ReplayAll()
    resp = self.callback()
    location = urllib.parse.unquote_plus(resp.headers['Location'])
    self.assertTrue(location.startswith(
      'http://localhost/#!Your Instagram account is private.'), location)

  def test_signup_multiple_profile_urls(self):
    self.expect_site_fetch()

    user = copy.deepcopy(PROFILE_USER)
    user['biography'] = 'http://a/ https://b'

    self.expect_webmention_discovery()

    self.mox.ReplayAll()
    resp = self.callback()
    self.assertEqual('http://localhost/instagram/snarfed', resp.headers['Location'])
    self.assertEqual(['snarfed.org', 'a', 'b'], self.inst.key.get().domains)

  def test_signup_state_0(self):
    """https://console.cloud.google.com/errors/5078670695812426116"""
    self.expect_site_fetch()
    self.expect_webmention_discovery()

    self.mox.ReplayAll()
    resp = self.callback(state='0')
    self.assertEqual('http://localhost/instagram/snarfed', resp.headers['Location'])

  def test_signup_instagram_blocks_fetch(self):
    self.expect_site_fetch()

    self.mox.ReplayAll()
    resp = self.callback()
    location = urllib.parse.unquote_plus(resp.headers['Location'])
    self.assertTrue(location.startswith('http://localhost/#'))
    self.assertIn('Apologies, Instagram is temporarily blocking us.', location)
