"""Unit tests for indieauth.py.
"""
import copy
import urllib.request, urllib.parse, urllib.error

from flask import get_flashed_messages
from oauth_dropins.webutil.testutil import TestCase
from oauth_dropins.webutil.util import json_dumps, json_loads
from oauth_dropins import indieauth
import requests

import indieauth as _  # just need to register the endpoints
from models import Domain
from . import testutil
import util


class IndieAuthTest(testutil.AppTest):

  def setUp(self):
    super().setUp()
    self.auth_entity = indieauth.IndieAuth(id='http://snarfed.org')

  def expect_indieauth_check(self):
    return TestCase.expect_requests_post(
      self, indieauth.INDIEAUTH_URL, 'me=http://snarfed.org', data={
        'me': 'http://snarfed.org',
        'state': 'towkin',
        'code': 'my_code',
        'client_id': indieauth.INDIEAUTH_CLIENT_ID,
        'redirect_uri': 'http://localhost/indieauth/callback',
      })

  def expect_site_fetch(self, body=None):
    if body is None:
      body = """
<html><body>
<a rel="me" href="https://www.instagram.com/snarfed">me on insta</a>
</body></html>
"""
    return TestCase.expect_requests_get(self, 'http://snarfed.org', body)

  def callback(self, token='towkin'):
    resp = self.client.get(
      '/indieauth/callback?code=my_code&state=%s' % util.encode_oauth_state({
        'endpoint': indieauth.INDIEAUTH_URL,
        'me': 'http://snarfed.org',
        'state': token,
      }))
    self.assertEqual(302, resp.status_code)
    return resp

  def test_callback_new_domain(self):
    self.expect_indieauth_check()
    self.expect_site_fetch()
    self.mox.ReplayAll()

    resp = self.callback()
    self.assertEqual('http://localhost/',resp.headers['Location'])
    self.assertEqual(['Authorized you for snarfed.org.'], get_flashed_messages())

    self.assert_entities_equal([
      Domain(id='snarfed.org', tokens=['towkin'], auth=self.auth_entity.key),
    ], Domain.query().fetch(), ignore=('created', 'updated'))

  def test_start_get(self):
    resp = self.client.get('/indieauth/start?token=foo')
    self.assertEqual(200, resp.status_code)

  def test_start_post(self):
    self.expect_site_fetch()
    self.mox.ReplayAll()

    resp = self.client.post('/indieauth/start', data={
      'token': 'foo',
      'me': 'http://snarfed.org',
    })
    self.assertEqual(302, resp.status_code)
    self.assertTrue(resp.headers['Location'].startswith(indieauth.INDIEAUTH_URL),
                    resp.headers['Location'])
