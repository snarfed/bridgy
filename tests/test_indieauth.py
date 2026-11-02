"""Unit tests for indieauth.py.
"""
from flask import get_flashed_messages
from webutil.testutil import requests_response
from oauth_dropins import indieauth
from oauth_dropins.models import PkceCode

import indieauth as _  # just need to register the endpoints
from models import Domain
from . import testutil


class IndieAuthTest(testutil.AppTest):

  def setUp(self):
    super().setUp()
    self.auth_entity = indieauth.IndieAuth(id='http://snarfed.org')

  def expect_indieauth_check(self):
    self.mock_post.return_value = requests_response('me=http://snarfed.org')

  def expect_site_fetch(self):
    self.mock_get.return_value = requests_response("""
<html><body>
<a rel="me" href="https://www.instagram.com/snarfed">me on insta</a>
</body></html>
""")

  def callback(self, token='towkin'):
    PkceCode(id='my_state', verifier='my_verifier', challenge='my_challenge',
             auth_endpoint=indieauth.INDIELOGIN_URL,
             token_endpoint=indieauth.INDIELOGIN_TOKEN_URL,
             me='http://snarfed.org', state=token).put()
    resp = self.client.get('/indieauth/callback', query_string={
      'code': 'my_code',
      'state': 'my_state',
      'iss': indieauth.INDIELOGIN_ISSUER,
    })
    self.assertEqual(302, resp.status_code)
    return resp

  def test_callback_new_domain(self):
    self.expect_indieauth_check()
    self.expect_site_fetch()

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

    resp = self.client.post('/indieauth/start', data={
      'token': 'foo',
      'me': 'http://snarfed.org',
    })
    self.assertEqual(302, resp.status_code)
    self.assertTrue(resp.headers['Location'].startswith(indieauth.INDIELOGIN_URL),
                    resp.headers['Location'])
    self.assert_requests_get('http://snarfed.org')
