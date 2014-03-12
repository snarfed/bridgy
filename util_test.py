# coding=utf-8
"""Unit tests for util.py.
"""

import json
import urlparse

import requests

from appengine_config import HTTP_TIMEOUT
import testutil
from testutil import FakeAuthEntity, FakeSource
import util

# the invisible character in the middle is an unusual unicode character
UNICODE_STR = u'a ✁ b'


class UtilTest(testutil.ModelsTest):

  def test_follow_redirects(self):
    self.mox.StubOutWithMock(requests, 'head', use_mock_anything=True)
    resp = requests.Response()
    resp.url = 'http://final/url'
    resp.headers['content-type'] = 'text/html'
    requests.head('http://will/redirect', allow_redirects=True, timeout=HTTP_TIMEOUT
                  ).AndReturn(resp)

    self.mox.ReplayAll()
    self.assert_equals('http://final/url',
                       util.follow_redirects('http://will/redirect').url)

    # the result should now be in memcache, so we shouldn't fetch the URL again
    self.assert_equals('http://final/url',
                       util.follow_redirects('http://will/redirect').url)

  def test_maybe_add_or_delete_source(self):
    # profile url with valid domain is required for publish
    for bad_url in None, 'not a url', 'http://fa.ke/xyz':
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
    params = urlparse.parse_qs(urlparse.urlparse(
        self.handler.response.headers['Location']).query)
    self.assertIn(UNICODE_STR, params['msg'][0].decode('utf-8'))

    for feature in None, '':
      src = self.handler.maybe_add_or_delete_source(FakeSource, auth_entity, feature)
      self.assertEquals([], src.features)
