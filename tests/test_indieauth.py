"""Unit tests for indieauth.py.
"""
import copy
import urllib.request, urllib.parse, urllib.error

from oauth_dropins.webutil.testutil import TestCase
from oauth_dropins.webutil.util import json_dumps, json_loads
from oauth_dropins import indieauth
import requests

import app
from models import Domain
from .testutil import ModelsTest
import util


class IndieAuthTest(ModelsTest):

  def setUp(self):
    super(IndieAuthTest, self).setUp()
    self.handler.messages = []
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
    resp = app.application.get_response(
      '/indieauth/callback?code=my_code&state=%s' % util.encode_oauth_state({
        'endpoint': indieauth.INDIEAUTH_URL,
        'me': 'http://snarfed.org',
        'state': token,
      }))
    self.assertEqual(302, resp.status_int)
    return resp

  def test_callback_new_domain(self):
    self.expect_indieauth_check()
    self.expect_site_fetch()
    self.mox.ReplayAll()

    resp = self.callback()
    self.assertEqual('http://localhost/#!Authorized you for snarfed.org.',
                     urllib.parse.unquote_plus(resp.headers['Location']))

    self.assert_entities_equal([
      Domain(id='snarfed.org', tokens=['towkin'], auth=self.auth_entity.key),
    ], Domain.query().fetch(), ignore=('created', 'updated'))
