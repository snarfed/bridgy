"""Unit tests for app.py.
"""

import urlparse

import app
import testutil
import util


class AppTest(testutil.ModelsTest):

  def test_poll_now(self):
    resp = app.application.get_response(
      '/poll-now', method='POST', body='key=%s' % self.sources[0].key.urlsafe())
    self.assertEquals(302, resp.status_int)
    self.assertEquals(self.sources[0].bridgy_url(self.handler),
                      resp.headers['Location'].split('?')[0])

  def test_poll_now_missing_key(self):
    resp = app.application.get_response('/poll-now', method='POST', body='')
    self.assertEquals(400, resp.status_int)
