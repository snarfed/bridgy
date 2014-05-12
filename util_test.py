# coding=utf-8
"""Unit tests for util.py.
"""

import json
import urllib
import urlparse

import requests

from appengine_config import HTTP_TIMEOUT
import testutil
from testutil import FakeAuthEntity, FakeSource
import util

# the invisible character in the middle is an unusual unicode character
UNICODE_STR = u'a ✁ b'


class UtilTest(testutil.ModelsTest):

  def setUp(self):
    super(testutil.ModelsTest, self).setUp()
    util.WEBMENTION_BLACKLIST.add('fa.ke')

  def test_follow_redirects(self):
    self.expect_requests_head('http://will/redirect',
                              redirected_url='http://final/url')
    self.mox.ReplayAll()
    self.assert_equals('http://final/url',
                       util.follow_redirects('http://will/redirect').url)

    # the result should now be in memcache, so we shouldn't fetch the URL again
    self.assert_equals('http://final/url',
                       util.follow_redirects('http://will/redirect').url)


  def test_follow_redirects_with_refresh_header(self):
    self.expect_requests_head('http://will/redirect',
                              response_headers={'refresh': '0; url=http://refresh'})
    self.expect_requests_head('http://refresh', redirected_url='http://final')

    self.mox.ReplayAll()
    self.assert_equals('http://final',
                       util.follow_redirects('http://will/redirect').url)

  def test_follow_redirects_defaults_scheme_to_http(self):
    self.expect_requests_head('http://foo/bar', redirected_url='http://final')
    self.mox.ReplayAll()
    self.assert_equals('http://final', util.follow_redirects('foo/bar').url)

  def test_maybe_add_or_delete_source(self):
    # profile url with valid domain is required for publish
    for bad_url in None, 'not>a<url', 'http://fa.ke/xyz':
      auth_entity = FakeAuthEntity(id='x', user_json=json.dumps({'url': bad_url}))
      auth_entity.put()
      self.assertIsNone(self.handler.maybe_add_or_delete_source(
          FakeSource, auth_entity, 'publish'))

    auth_entity = FakeAuthEntity(id='x', user_json=json.dumps(
        {'url': 'http://foo.com/', 'name': UNICODE_STR}))
    auth_entity.put()
    src = self.handler.maybe_add_or_delete_source(FakeSource, auth_entity, 'publish')
    self.assertEquals(['publish'], src.features)

    self.assertEquals(302, self.handler.response.status_int)
    parsed = urlparse.urlparse(self.handler.response.headers['Location'])
    self.assertIn(UNICODE_STR, urllib.unquote_plus(parsed.fragment).decode('utf-8'))

    for feature in None, '':
      src = self.handler.maybe_add_or_delete_source(FakeSource, auth_entity, feature)
      self.assertEquals([], src.features)

  def test_prune_activity(self):
    for orig, expected in (
      ({'id': 1, 'content': 'X', 'foo': 'bar'}, {'id': 1, 'content': 'X'}),
      ({'id': 1, 'object': {'objectType': 'note'}}, {'id': 1}),
      ({'id': 1, 'object': {'url': 'http://xyz'}},) * 2,  # no change
      ({'to': [{'objectType': 'group', 'alias': '@public'}]}, {}),
      ({'object': {'to': [{'objectType': 'group', 'alias': '@private'}]}},) * 2,
      ({'id': 1, 'object': {'id': 1}}, {'id': 1}),
      ({'id': 1, 'object': {'id': 2}},) * 2,
      ):
      self.assert_equals(expected, util.prune_activity(orig))
