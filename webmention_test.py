"""Unit tests for webmention.py.
"""

__author__ = ['Ryan Barrett <activitystreams@ryanb.org>']

import json
import mox
import urllib

import webmention
from webutil import testutil
import webapp2


class WebmentionTest(testutil.HandlerTest):

  def test_webmention(self):
    self.expect_urlopen('http://pin13.net/mf2/?url=%s' %
                        urllib.quote_plus('http://foo.com/bar'),
                        json.dumps({}))
    self.mox.ReplayAll()

    resp = webmention.application.get_response(
      '/webmention', method='POST',
      body='source=http://foo.com/bar&target=http://facebook.com/123')

    self.assertEqual(200, resp.status_int)

