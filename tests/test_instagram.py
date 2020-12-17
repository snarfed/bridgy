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
from instagram import Instagram
from .testutil import ModelsTest, instagram_profile_user
import util


PROFILE_USER = copy.deepcopy(
  gr_test_instagram.HTML_PROFILE['entry_data']['ProfilePage'][0]['graphql']['user'])
PROFILE_USER['id'] = '987'


class InstagramTest(ModelsTest):

  def setUp(self):
    super(InstagramTest, self).setUp()
    self.handler.messages = []
    self.inst = Instagram.new(
      self.handler, actor={
        'objectType': 'person',
        'id': 'tag:instagram.com,2013:420973239',
        'username': 'snarfed',
        'displayName': 'Ryan Barrett',
        'url': 'https://snarfed.org/',
        'image': {'url': 'http://pic.ture/url'},
        # ...
      })

  def expect_webmention_discovery(self):
    return self.expect_requests_get('https://snarfed.org/', '', stream=None,
                                    verify=False)

  def test_new(self):
    self.assertIsNone(self.inst.auth_entity)
    self.assertEqual('snarfed', self.inst.key.string_id())
    self.assertEqual('http://pic.ture/url', self.inst.picture)
    self.assertEqual('https://www.instagram.com/snarfed/', self.inst.url)
    self.assertEqual('https://www.instagram.com/snarfed/', self.inst.silo_url())
    # self.assertEqual('tag:instagram.com,2013:420973239', self.inst.user_tag_id())
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

  def test_homepage(self):
    resp = app.application.get_response(
      '/instagram/browser/homepage', method='POST',
      text=gr_test_instagram.HTML_FEED_COMPLETE)
    self.assertEqual(200, resp.status_int)
    self.assertEqual('snarfed', resp.text)

  def test_homepage_bad_html(self):
    resp = app.application.get_response(
      '/instagram/browser/homepage', method='POST',
      text='not a logged in IG feed')
    self.assertEqual(400, resp.status_int)

  def test_profile_new_user(self):
    self.assertIsNone(Instagram.get_by_id('snarfed'))

    self.expect_webmention_discovery()
    self.mox.ReplayAll()

    resp = app.application.get_response(
      '/instagram/browser/profile', method='POST',
      text=gr_test_instagram.HTML_PROFILE_COMPLETE)

    self.assertEqual(200, resp.status_int)
    self.assertEqual([
      'https://www.instagram.com/p/ABC123/',
      'https://www.instagram.com/p/XYZ789/',
    ], resp.json)

    ig = Instagram.get_by_id('snarfed')
    self.assertEqual('Ryan B', ig.name)
    self.assertEqual('https://scontent-sjc2-1.cdninstagram.com/hphotos-xfa1/t51.2885-19/11373714_959073410822287_2004790583_a.jpg', ig.picture)
    self.assertEqual('https://www.instagram.com/snarfed/', ig.url)
    self.assertEqual(['https://snarfed.org/'], ig.domain_urls)
    self.assertEqual(['snarfed.org'], ig.domains)

  def test_profile_private_account(self):
    resp = app.application.get_response(
      '/instagram/browser/profile', method='POST',
      text=gr_test_instagram.HTML_PROFILE_PRIVATE_COMPLETE)
    self.assertEqual(400, resp.status_int)

  # def test_signup_multiple_profile_urls(self):
  #   self.expect_site_fetch()

  #   user = copy.deepcopy(PROFILE_USER)
  #   user['biography'] = 'http://a/ https://b'

  #   self.expect_webmention_discovery()

  #   self.mox.ReplayAll()
  #   resp = self.callback()
  #   self.assertEqual('http://localhost/instagram/snarfed', resp.headers['Location'])
  #   self.assertEqual(['snarfed.org', 'a', 'b'], self.inst.key.get().domains)
