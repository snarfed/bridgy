"""Unit tests for publish.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import json
import mox
import urllib

from activitystreams.oauth_dropins.webutil import testutil
import publish
import webapp2


class PublishTest(testutil.HandlerTest):

  def _test_publish(self):
    self.expect_urlopen('http://pin13.net/mf2/?url=%s' %
                        urllib.quote_plus('http://foo.com/bar'),
                        json.dumps({}))
    self.mox.ReplayAll()

    resp = publish.application.get_response(
      '/publish', method='POST',
      body='source=http://foo.com/bar&target=http://facebook.com/123')

    self.assertEqual(403, resp.status_int)

