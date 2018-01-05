"""Unit tests for instagram.py.
"""
import copy
import json
import urllib

from oauth_dropins import indieauth
from oauth_dropins.webutil.testutil import TestCase
from granary import instagram as gr_instagram
from granary.test import test_instagram
import requests

import appengine_config
import instagram
import testutil
import util


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

    self.bridgy_api_state = {
      # dash in this URL is regression test for
      # https://console.cloud.google.com/errors/8827591112854923168
      'callback': 'http://my.site/call-back',
      'feature': 'listen,publish',
      'operation': 'add',
      'user_url': 'http://snarfed.org',
    }

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

  def expect_indieauth_check(self, state=''):
    TestCase.expect_requests_post(
      self, indieauth.INDIEAUTH_URL, 'me=http://snarfed.org', data={
        'me': 'http://snarfed.org',
        'state': state,
        'code': 'my_code',
        'client_id': appengine_config.INDIEAUTH_CLIENT_ID,
        'redirect_uri': 'http://localhost/instagram/callback',
      })

  def expect_instagram_fetch(self, body=test_instagram.HTML_PROFILE_COMPLETE,
                             **kwargs):
    TestCase.expect_requests_get(self, gr_instagram.HTML_BASE_URL + 'snarfed/',
                                 body, allow_redirects=False, **kwargs)

  def expect_webmention_discovery(self):
    self.expect_requests_get('https://snarfed.org', '', stream=None, verify=False)

  def callback(self, state=''):
    resp = instagram.application.get_response(
      '/instagram/callback?code=my_code&state=%s' % util.encode_oauth_state({
        'endpoint': indieauth.INDIEAUTH_URL,
        'me': 'http://snarfed.org',
        'state': state,
      }))
    self.assertEquals(302, resp.status_int)
    return resp

  def test_signup_success(self):
    self.expect_site_fetch()
    self.expect_indieauth_check()
    self.expect_instagram_fetch()
    self.expect_webmention_discovery()

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

  def test_signup_multiple_profile_urls(self):
    self.expect_site_fetch()
    self.expect_indieauth_check()

    profile = copy.deepcopy(test_instagram.HTML_PROFILE)
    profile['entry_data']['ProfilePage'][0]['user']['biography'] = \
      'http://a/ https://b'
    self.expect_instagram_fetch(
      test_instagram.HTML_HEADER + json.dumps(profile) + test_instagram.HTML_FOOTER)

    self.expect_webmention_discovery()

    self.mox.ReplayAll()
    resp = self.callback()
    self.assertEquals('http://localhost/instagram/snarfed', resp.headers['Location'])
    self.assertEquals(['snarfed.org', 'a', 'b'], self.inst.key.get().domains)

  def test_signup_state_0(self):
    """https://console.cloud.google.com/errors/5078670695812426116"""
    self.expect_site_fetch()
    self.expect_indieauth_check(state='0')
    self.expect_instagram_fetch()
    self.expect_webmention_discovery()

    self.mox.ReplayAll()
    resp = self.callback(state='0')
    self.assertEquals('http://localhost/instagram/snarfed', resp.headers['Location'])

  def test_gr_source_scrape(self):
    self.assertTrue(self.inst.gr_source.scrape)

  def test_registration_api_start_handler_post(self):
    self.expect_site_fetch()
    self.mox.ReplayAll()
    resp = instagram.application.get_response(
      '/instagram/start', method='POST', body=urllib.urlencode(self.bridgy_api_state))

    self.assertEquals(302, resp.status_code)

    state_json = util.encode_oauth_state(self.bridgy_api_state)
    expected_auth_url = indieauth.INDIEAUTH_URL + '?' + urllib.urlencode({
      'me': 'http://snarfed.org',
      'client_id': appengine_config.INDIEAUTH_CLIENT_ID,
      'redirect_uri': 'http://localhost/instagram/callback',
      'state': util.encode_oauth_state({
        'endpoint': indieauth.INDIEAUTH_URL,
        'me': 'http://snarfed.org',
        'state': state_json,
      }),
    })
    self.assertEquals(expected_auth_url, resp.headers['Location'])

  def test_registration_api_start_handler_site_fetch_fails(self):
    # Use e.g. https://badssl.com/ for manual testing.
    self.expect_site_fetch('').AndRaise(
      requests.exceptions.SSLError('Bad SSL for xyz.com'))
    self.mox.ReplayAll()

    resp = instagram.application.get_response(
      '/instagram/start', method='POST', body=urllib.urlencode(self.bridgy_api_state))
    self.assertEquals(302, resp.status_code)
    location = urllib.unquote_plus(resp.headers['Location'])
    self.assertTrue(location.startswith(
      "http://localhost/#!Couldn't fetch your web site: Bad SSL for xyz.com"), location)

  def test_registration_api_finish_success(self):
    state = util.encode_oauth_state(self.bridgy_api_state)
    self.expect_indieauth_check(state=state)
    self.expect_site_fetch()
    self.expect_instagram_fetch()
    self.expect_webmention_discovery()

    self.mox.ReplayAll()
    resp = self.callback(state=urllib.quote_plus(state))
    self.assertEquals(302, resp.status_int)
    self.assertEquals('http://my.site/call-back?' + urllib.urlencode({
      'result': 'success',
      'key': self.inst.key.urlsafe(),
      'user': 'http://localhost/instagram/snarfed',
    }), resp.headers['Location'])

  def test_registration_api_finish_no_rel_me(self):
    state = util.encode_oauth_state(self.bridgy_api_state)
    self.expect_indieauth_check(state=state)
    self.expect_site_fetch('')

    self.mox.ReplayAll()
    resp = self.callback(state=urllib.quote_plus(state))
    self.assertEquals(302, resp.status_int)
    location = urllib.unquote_plus(resp.headers['Location'])
    self.assertTrue(location.startswith(
      'http://localhost/#!No Instagram profile found.'), location)
