"""Unit tests for instagram.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import copy
import json
import urllib

from oauth_dropins import indieauth
from oauth_dropins.webutil.testutil import TestCase
from granary.test import test_instagram

import appengine_config
import instagram
import testutil


class InstagramTest(testutil.ModelsTest):

  def setUp(self):
    super(InstagramTest, self).setUp()
    self.handler.messages = []
    self.auth_entity = indieauth.IndieAuth(id='http://foo.com', user_json=json.dumps({
      'rel-me': ['http://instagram.com/snarfed'],
    }))
    self.inst = instagram.Instagram.new(
      self.handler, auth_entity=self.auth_entity, actor={
        'username': 'snarfed',
        'displayName': 'Ryan Barrett',
        'image': {'url': 'http://pic.ture/url'},
      })

  def test_new(self):
    self.assertEqual(self.auth_entity, self.inst.auth_entity.get())
    self.assertEqual('snarfed', self.inst.key.string_id())
    self.assertEqual('http://pic.ture/url', self.inst.picture)
    self.assertEqual('https://www.instagram.com/snarfed/', self.inst.url)
    self.assertEqual('https://www.instagram.com/snarfed/', self.inst.silo_url())
    self.assertEqual('tag:instagram.com,2013:snarfed', self.inst.user_tag_id())
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

  def expect_site_fetch(self, body=None):
    if body is None:
      body = """
<html><body>
<a rel="me" href="https://www.instagram.com/snarfed">me on insta</a>
</body></html>
"""
    TestCase.expect_requests_get(self, 'http://snarfed.org', body)

  def expect_indieauth_check(self):
    TestCase.expect_requests_post(
      self, indieauth.INDIEAUTH_URL, 'me=http://snarfed.org', data={
        'me': 'http://snarfed.org',
        'state': json.dumps({'feature': 'listen', 'operation': 'add'}),
        'code': 'my_code',
        'client_id': appengine_config.INDIEAUTH_CLIENT_ID,
        'redirect_uri': 'http://localhost/instagram/callback',
      })

  def expect_instagram_fetch(self, body=test_instagram.HTML_PROFILE_COMPLETE,
                             **kwargs):
    TestCase.expect_requests_get(self, 'https://www.instagram.com/snarfed/',
                                 body, allow_redirects=False, **kwargs)

  def callback(self):
    resp = instagram.application.get_response(
      '/instagram/callback?me=http://snarfed.org&code=my_code&state=%s' %
      urllib.quote_plus(json.dumps({'feature': 'listen', 'operation': 'add'})))
    self.assertEquals(302, resp.status_int)
    return resp

  def test_signup_success(self):
    self.expect_site_fetch()
    self.expect_indieauth_check()
    self.expect_instagram_fetch()

    # the signup attempt to discover my webmention endpoint
    self.expect_requests_get('https://snarfed.org/', '', stream=None, verify=False)

    self.mox.ReplayAll()
    resp = self.callback()
    self.assertEquals('http://localhost/instagram/snarfed', resp.headers['Location'])

  def test_signup_no_rel_me(self):
    self.expect_site_fetch('')
    self.expect_indieauth_check()

    self.mox.ReplayAll()
    resp = self.callback()
    location = urllib.unquote_plus(resp.headers['Location'])
    self.assertTrue(location.startswith(
      'http://localhost/#!No Instagram profile found.'), location)

  def test_signup_no_instagram_profile(self):
    self.expect_site_fetch()
    self.expect_indieauth_check()
    self.expect_instagram_fetch('', status_code=404)

    self.mox.ReplayAll()
    resp = self.callback()
    location = urllib.unquote_plus(resp.headers['Location'])
    self.assertTrue(location.startswith(
      "http://localhost/#!Couldn't find Instagram user 'snarfed'"), location)

  def test_signup_no_instagram_profile_backlink(self):
    self.expect_site_fetch()
    self.expect_indieauth_check()

    profile = copy.deepcopy(test_instagram.HTML_PROFILE)
    del profile['entry_data']['ProfilePage'][0]['user']['external_url']
    self.expect_instagram_fetch(
      test_instagram.HTML_HEADER + json.dumps(profile) + test_instagram.HTML_FOOTER)

    self.mox.ReplayAll()
    resp = self.callback()
    location = urllib.unquote_plus(resp.headers['Location'])
    self.assertTrue(location.startswith(
      'http://localhost/#!Please add https://snarfed.org to your Instagram'), location)

  def test_signup_private_account(self):
    self.expect_site_fetch()
    self.expect_indieauth_check()

    profile = copy.deepcopy(test_instagram.HTML_PROFILE)
    self.expect_instagram_fetch(test_instagram.HTML_PROFILE_PRIVATE_COMPLETE)

    self.mox.ReplayAll()
    resp = self.callback()
    location = urllib.unquote_plus(resp.headers['Location'])
    self.assertTrue(location.startswith(
      'http://localhost/#!Your Instagram account is private.'), location)
