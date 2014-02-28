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

  def assert_error(self, expected_error, source='http://source',
                   target='http://brid.gy/publish/facebook'):
    resp = publish.application.get_response(
      '/publish/webmention', method='POST',
      body='source=%s&target=%s' % (source, target))
    self.assertEquals(400, resp.status_int)
    self.assertEquals(expected_error, json.loads(resp.body)['error'])

  def test_bad_target_url(self):
    self.assert_error('Target must be brid.gy/publish/{facebook,twitter}',
                        target='foo')

  def test_unsupported_source(self):
    self.assert_error('Sorry, Instagram is not yet supported.',
                      target='http://brid.gy/publish/instagram')
