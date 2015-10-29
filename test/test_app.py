"""Unit tests for app.py.
"""
import datetime
import json
import urllib

from google.appengine.api import memcache
from google.appengine.ext import ndb
from oauth_dropins import handlers as oauth_handlers
import mf2py
import webapp2

import app
import models
import util
import testutil
from testutil import FakeAuthEntity


# this class stands in for a oauth_dropins module
class FakeOAuthHandlerModule:
  StartHandler = testutil.OAuthStartHandler


class AppTest(testutil.ModelsTest):

  def test_poll_now(self):
    self.assertEqual([], self.taskqueue_stub.GetTasks('poll'))

    key = self.sources[0].key.urlsafe()
    resp = app.application.get_response('/poll-now', method='POST', body='key=' + key)
    self.assertEquals(302, resp.status_int)
    self.assertEquals(self.sources[0].bridgy_url(self.handler),
                      resp.headers['Location'].split('#')[0])
    params = testutil.get_task_params(self.taskqueue_stub.GetTasks('poll-now')[0])
    self.assertEqual(key, params['source_key'])

  def test_retry(self):
    self.assertEqual([], self.taskqueue_stub.GetTasks('propagate'))

    source = self.sources[0]
    source.domain_urls = ['http://orig']
    source.put()

    resp = self.responses[0]
    resp.status = 'complete'
    resp.unsent = ['http://unsent']
    resp.sent = ['http://sent']
    resp.error = ['http://error']
    resp.failed = ['http://failed']
    resp.skipped = ['https://skipped']

    # SyndicatedPost with new target URLs
    resp.activities_json = [
      json.dumps({'object': {'url': 'https://silo/1'}}),
      json.dumps({'url': 'https://silo/2', 'object': {'unused': 'ok'}}),
      json.dumps({'url': 'https://silo/3'}),
    ]
    resp.put()
    models.SyndicatedPost.insert(source, 'https://silo/1', 'https://orig/1')
    models.SyndicatedPost.insert(source, 'https://silo/2', 'http://orig/2')
    models.SyndicatedPost.insert(source, 'https://silo/3', 'http://orig/3')

    # cached webmention endpoint
    memcache.set('W https skipped', 'asdf')

    key = resp.key.urlsafe()
    response = app.application.get_response(
      '/retry', method='POST', body='key=' + key)
    self.assertEquals(302, response.status_int)
    self.assertEquals(source.bridgy_url(self.handler),
                      response.headers['Location'].split('#')[0])
    params = testutil.get_task_params(self.taskqueue_stub.GetTasks('propagate')[0])
    self.assertEqual(key, params['response_key'])

    # status and URLs should be refreshed
    got = resp.key.get()
    self.assertEqual('new', got.status)
    self.assertItemsEqual(
      ['http://unsent', 'http://sent', 'https://skipped', 'http://error',
       'http://failed', 'https://orig/1', 'http://orig/2', 'http://orig/3'],
      got.unsent)
    for field in got.sent, got.skipped, got.error, got.failed:
      self.assertEqual([], field)

    # webmention endpoints for URL domains should be refreshed
    self.assertIsNone(memcache.get('W https skipped'))

  def test_retry_redirect_to(self):
    key = self.responses[0].put()
    response = app.application.get_response(
      '/retry', method='POST', body='key=%s&redirect_to=/foo/bar' % key.urlsafe())
    self.assertEquals(302, response.status_int)
    self.assertEquals('http://localhost/foo/bar',
                      response.headers['Location'].split('#')[0])

  def test_poll_now_and_retry_response_missing_key(self):
    for endpoint in '/poll-now', '/retry':
      for body in '', 'key=' + self.responses[0].key.urlsafe():  # hasn't been stored
        resp = app.application.get_response(endpoint, method='POST', body=body)
        self.assertEquals(400, resp.status_int)

  def test_delete_source_callback(self):
    app.DeleteStartHandler.OAUTH_MODULES['FakeSource'] = FakeOAuthHandlerModule

    auth_entity_key = self.sources[0].auth_entity.urlsafe()
    key = self.sources[0].key.urlsafe()

    resp = app.application.get_response(
      '/delete/start', method='POST', body=urllib.urlencode({
        'feature': 'listen',
        'key': key,
        'callback': 'http://withknown.com/bridgy_callback',
      }))

    encoded_state = urllib.quote_plus(
      '{"callback":"http://withknown.com/bridgy_callback",'
      '"feature":"listen","operation":"delete","source":"' + key + '"}')

    # when silo oauth is done, it should send us back to /SOURCE/delete/finish,
    # which would in turn redirect to the more general /delete/finish.
    expected_auth_url = 'http://fake/auth/url?' + urllib.urlencode({
      'redirect_uri': 'http://localhost/fake/delete/finish?state='
      + encoded_state,
    })

    self.assertEquals(302, resp.status_int)
    self.assertEquals(expected_auth_url, resp.headers['Location'])

    # assume that the silo auth finishes and redirects to /delete/finish
    resp = app.application.get_response(
      '/delete/finish?'
      + 'auth_entity=' + auth_entity_key
      + '&state=' + encoded_state)

    self.assertEquals(302, resp.status_int)
    self.assertEquals(
      'http://withknown.com/bridgy_callback?' + urllib.urlencode([
        ('result', 'success'),
        ('key', ndb.Key('FakeSource', '0123456789').urlsafe()),
        ('user', 'http://localhost/fake/0123456789')
      ]), resp.headers['Location'])

  def test_delete_source_declined(self):
    app.DeleteStartHandler.OAUTH_MODULES['FakeSource'] = FakeOAuthHandlerModule

    key = self.sources[0].key.urlsafe()
    resp = app.application.get_response(
      '/delete/start', method='POST', body=urllib.urlencode({
        'feature': 'listen',
        'key': key,
        'callback': 'http://withknown.com/bridgy_callback',
      }))

    encoded_state = urllib.quote_plus(
      '{"callback":"http://withknown.com/bridgy_callback",'
      '"feature":"listen","operation":"delete","source":"' + key + '"}')

    # when silo oauth is done, it should send us back to /SOURCE/delete/finish,
    # which would in turn redirect to the more general /delete/finish.
    expected_auth_url = 'http://fake/auth/url?' + urllib.urlencode({
      'redirect_uri': 'http://localhost/fake/delete/finish?state='
      + encoded_state,
    })

    self.assertEquals(302, resp.status_int)
    self.assertEquals(expected_auth_url, resp.headers['Location'])

    # assume that the silo auth finishes
    resp = app.application.get_response(
      '/delete/finish?declined=True&state=' + encoded_state)

    self.assertEquals(302, resp.status_int)
    self.assertEquals(
      'http://withknown.com/bridgy_callback?' + urllib.urlencode([
        ('result', 'declined')
      ]), resp.headers['Location'])

  def test_user_page(self):
    resp = app.application.get_response(self.sources[0].bridgy_path())
    self.assertEquals(200, resp.status_int)

  def test_user_page_trailing_slash(self):
    resp = app.application.get_response(self.sources[0].bridgy_path() + '/')
    self.assertEquals(200, resp.status_int)

  def test_user_page_lookup_with_username_etc(self):
    self.sources[0].name = 'FooBar'
    self.sources[0].domains = ['foox.com']
    self.sources[0].put()

    for id in 'FooBar', 'foox.com':
      resp = app.application.get_response('/fake/%s' % id)
      self.assertEquals(301, resp.status_int)
      self.assertEquals('http://localhost/fake/%s' % self.sources[0].key.id(),
                        resp.headers['Location'])

    resp = app.application.get_response('/fake/nope')
    self.assertEquals(404, resp.status_int)

  def test_user_page_with_no_features_404s(self):
    self.sources[0].features = []
    self.sources[0].put()

    resp = app.application.get_response(self.sources[0].bridgy_path())
    self.assertEquals(404, resp.status_int)

  def test_user_page_mf2(self):
    """parsing the user page with mf2 gives some informative fields
    about the user and their Bridgy account status.
    """
    user_url = self.sources[0].bridgy_path()
    resp = app.application.get_response(user_url)
    self.assertEquals(200, resp.status_int)
    parsed = mf2py.Parser(url=user_url, doc=resp.body).to_dict()
    hcard = parsed.get('items', [])[0]
    self.assertEquals(['h-card'], hcard['type'])
    self.assertEquals(
      ['Fake User'], hcard['properties'].get('name'))
    self.assertEquals(
      ['http://fa.ke/profile/url'], hcard['properties'].get('url'))
    self.assertEquals(
      ['enabled'], hcard['properties'].get('bridgy-account-status'))
    self.assertEquals(
      ['enabled'], hcard['properties'].get('bridgy-listen-status'))
    self.assertEquals(
      ['disabled'], hcard['properties'].get('bridgy-publish-status'))

  def test_logout(self):
    util.now_fn = lambda: datetime.datetime(2000, 1, 1)
    resp = app.application.get_response('/logout')
    self.assertEquals('logins=; expires=2001-12-31 00:00:00; Path=/',
                      resp.headers['Set-Cookie'])
    self.assertEquals(302, resp.status_int)
    self.assertEquals('http://localhost/#!Logged%20out.', resp.headers['Location'])

