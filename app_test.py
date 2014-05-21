"""Unit tests for app.py.
"""

import urlparse

import app
import testutil
import util


class AppTest(testutil.ModelsTest):

  def test_poll_now(self):
    self.assertEqual([], self.taskqueue_stub.GetTasks('poll'))

    key = self.sources[0].key.urlsafe()
    resp = app.application.get_response('/poll-now', method='POST', body='key=' + key)
    self.assertEquals(302, resp.status_int)
    self.assertEquals(self.sources[0].bridgy_url(self.handler),
                      resp.headers['Location'].split('#')[0])
    params = testutil.get_task_params(self.taskqueue_stub.GetTasks('poll')[0])
    self.assertEqual(key, params['source_key'])

  def test_retry_response(self):
    self.assertEqual([], self.taskqueue_stub.GetTasks('propagate'))

    self.responses[0].put()
    key = self.responses[0].key.urlsafe()
    resp = app.application.get_response(
      '/retry', method='POST', body='key=' + key)
    self.assertEquals(302, resp.status_int)
    self.assertEquals(self.sources[0].bridgy_url(self.handler),
                      resp.headers['Location'].split('#')[0])
    params = testutil.get_task_params(self.taskqueue_stub.GetTasks('propagate')[0])
    self.assertEqual(key, params['response_key'])

  def test_poll_now_and_retry_response_missing_key(self):
    for endpoint in '/poll-now', '/retry':
      for body in '', 'key=' + self.responses[0].key.urlsafe():  # hasn't been stored
        resp = app.application.get_response(endpoint, method='POST', body=body)
        self.assertEquals(400, resp.status_int)
