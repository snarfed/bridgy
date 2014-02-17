"""Unit tests for util.py.
"""

import requests

from appengine_config import HTTP_TIMEOUT
import testutil
import util


class UtilTest(testutil.HandlerTest):

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
