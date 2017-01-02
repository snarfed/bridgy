# coding=utf-8
"""Unit tests for app.py.
"""
import datetime
import json
import urllib
import urlparse

from google.appengine.api import memcache
from google.appengine.ext import ndb
import mox
from oauth_dropins import handlers as oauth_handlers
from oauth_dropins.twitter import TwitterAuth
import tweepy
import webapp2

import app
import models
from models import Publish, PublishedPage
import util
import testutil
import twitter


# this class stands in for a oauth_dropins module
class FakeOAuthHandlerModule:
  StartHandler = testutil.OAuthStartHandler


class AppTest(testutil.ModelsTest):

  def setUp(self):
    super(AppTest, self).setUp()
    util.now_fn = lambda: testutil.NOW

  def test_front_page(self):
    self.assertEquals(0, util.CachedPage.query().count())

    resp = app.application.get_response('/')
    self.assertEquals(200, resp.status_int)
    self.assertEquals('no-cache', resp.headers['Cache-Control'])

    cached = util.CachedPage.get_by_id('/')
    self.assert_multiline_equals(resp.body, cached.html)

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
    source.last_hfeed_refetch = last_hfeed_refetch = \
        testutil.NOW - datetime.timedelta(minutes=1)
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
      json.dumps({'object': {'url': 'https://fa.ke/1'}}),
      json.dumps({'url': 'https://fa.ke/2', 'object': {'unused': 'ok'}}),
      json.dumps({'url': 'https://fa.ke/3'}),
    ]
    resp.put()
    models.SyndicatedPost.insert(source, 'https://fa.ke/1', 'https://orig/1')
    models.SyndicatedPost.insert(source, 'https://fa.ke/2', 'http://orig/2')
    models.SyndicatedPost.insert(source, 'https://fa.ke/3', 'http://orig/3')

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

    # shouldn't have refetched h-feed
    self.assertEqual(last_hfeed_refetch, source.key.get().last_hfeed_refetch)

  def test_retry_redirect_to(self):
    key = self.responses[0].put()
    response = app.application.get_response(
      '/retry', method='POST', body='key=%s&redirect_to=/foo/bar' % key.urlsafe())
    self.assertEquals(302, response.status_int)
    self.assertEquals('http://localhost/foo/bar',
                      response.headers['Location'].split('#')[0])

  def test_crawl_now(self):
    source = self.sources[0]
    source.domain_urls = ['http://orig']
    source.last_hfeed_refetch = source.last_feed_syndication_url = testutil.NOW
    source.put()

    key = source.key.urlsafe()
    response = app.application.get_response(
      '/crawl-now', method='POST', body='key=%s' % key)
    self.assertEquals(source.bridgy_url(self.handler),
                      response.headers['Location'].split('#')[0])
    self.assertEquals(302, response.status_int)

    params = testutil.get_task_params(self.taskqueue_stub.GetTasks('poll-now')[0])
    self.assertEqual(key, params['source_key'])

    source = source.key.get()
    self.assertEqual(models.REFETCH_HFEED_TRIGGER, source.last_hfeed_refetch)
    self.assertIsNone(source.last_feed_syndication_url)

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

  def test_delete_start_redirect_url_error(self):
    app.DeleteStartHandler.OAUTH_MODULES['FakeSource'] = FakeOAuthHandlerModule
    self.mox.StubOutWithMock(FakeOAuthHandlerModule.StartHandler, 'redirect_url')
    FakeOAuthHandlerModule.StartHandler.redirect_url(state=mox.IgnoreArg()
      ).AndRaise(tweepy.TweepError('Connection closed unexpectedly...'))
    self.mox.ReplayAll()

    resp = app.application.get_response(
      '/delete/start', method='POST', body=urllib.urlencode({
        'feature': 'listen',
        'key': self.sources[0].key.urlsafe(),
      }))
    self.assertEquals(302, resp.status_int)
    location = urlparse.urlparse(resp.headers['Location'])
    self.assertEquals('/fake/0123456789', location.path)
    self.assertEquals('!FakeSource API error -: Connection closed unexpectedly...',
                      urllib.unquote(location.fragment))

  def test_delete_removes_from_logins_cookie(self):
    cookie = ('logins="/fake/%s?Fake%%20User|/other/1?bob"; '
              'expires=2001-12-31 00:00:00; Path=/' % self.sources[0].key.id())

    state = self.handler.construct_state_param_for_add(
      feature='listen', operation='delete', source=self.sources[0].key.urlsafe())
    resp = app.application.get_response(
      '/delete/finish?auth_entity=%s&state=%s' %
      (self.sources[0].auth_entity.urlsafe(), state),
      headers={'Cookie': cookie})

    self.assertEquals(302, resp.status_int)
    location = resp.headers['Location']
    self.assertTrue(location.startswith('http://localhost/#'), location)
    new_cookie = resp.headers['Set-Cookie']
    self.assertTrue(new_cookie.startswith('logins="/other/1?bob"; '), new_cookie)

  def test_user_page(self):
    resp = app.application.get_response(self.sources[0].bridgy_path())
    self.assertEquals(200, resp.status_int)
    self.assertEquals('no-cache', resp.headers['Cache-Control'])

  def test_user_page_trailing_slash(self):
    resp = app.application.get_response(self.sources[0].bridgy_path() + '/')
    self.assertEquals(200, resp.status_int)

  def test_user_page_lookup_with_username_etc(self):
    self.sources[0].username = 'FooBar'
    self.sources[0].name = u'Snoøpy Barrett'
    self.sources[0].domains = ['foox.com']
    self.sources[0].put()

    for id in 'FooBar', u'Snoøpy Barrett', 'foox.com':
      resp = app.application.get_response('/fake/%s' % urllib.quote(id.encode('utf-8')))
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

  def test_social_user_page_mf2(self):
    """Check the custom mf2 we render on social user pages."""
    self.sources[0].features = ['listen', 'publish']
    self.sources[0].put()
    for entity in self.responses + self.publishes + self.blogposts:
      entity.put()

    user_url = self.sources[0].bridgy_path()
    resp = app.application.get_response(user_url)
    self.assertEquals(200, resp.status_int)

    parsed = util.mf2py_parse(resp.body, user_url)
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
      ['enabled'], hcard['properties'].get('bridgy-publish-status'))

    expected_resps = self.responses[:10]
    for item, resp in zip(hcard['children'], expected_resps):
      self.assertIn('h-bridgy-response', item['type'])
      props = item['properties']
      self.assertEquals([resp.status], props['bridgy-status'])
      self.assertEquals([json.loads(resp.activities_json[0])['url']],
                        props['bridgy-original-source'])
      self.assertEquals(resp.unsent, props['bridgy-target'])

    publish = hcard['children'][len(expected_resps)]
    self.assertIn('h-bridgy-publish', publish['type'])
    props = publish['properties']
    self.assertEquals([self.publishes[0].key.parent().id()], props['url'])
    self.assertEquals([self.publishes[0].status], props['bridgy-status'])

  def test_user_page_private_twitter(self):
    auth_entity = TwitterAuth(
      id='foo',
      user_json=json.dumps({'protected': True}),
      token_key='', token_secret='',
    ).put()
    tw = twitter.Twitter(id='foo', auth_entity=auth_entity, features=['listen'])
    tw.put()

    resp = app.application.get_response(tw.bridgy_path())
    self.assertEquals(200, resp.status_int)
    self.assertIn('Your Twitter account is private!', resp.body)
    self.assertNotIn('most of your recent posts are private', resp.body)

  def test_user_page_recent_private_posts(self):
    self.sources[0].recent_private_posts = app.RECENT_PRIVATE_POSTS_THRESHOLD
    self.sources[0].put()

    resp = app.application.get_response(self.sources[0].bridgy_path())
    self.assertEquals(200, resp.status_int)
    self.assertIn('most of your recent posts are private', resp.body)

  def test_user_page_publish_url_with_unicode_char(self):
    """Check the custom mf2 we render on social user pages."""
    self.sources[0].features = ['publish']
    self.sources[0].put()

    url = u'https://ptt.com/ransomw…ocks-user-access/'
    Publish(parent=PublishedPage(id=url.encode('utf-8')).key,
            source=self.sources[0].key).put()

    user_url = self.sources[0].bridgy_path()
    resp = app.application.get_response(user_url)
    self.assertEquals(200, resp.status_int)

    parsed = util.mf2py_parse(resp.body, user_url)
    publish = parsed['items'][0]['children'][0]

  def test_users_page(self):
    resp = app.application.get_response('/users')
    for source in self.sources:
      self.assertIn(
        '<a href="%s" title="%s"' % (source.bridgy_path(), source.label()),
        resp.body)
    self.assertEquals(200, resp.status_int)

  def test_logout(self):
    util.now_fn = lambda: datetime.datetime(2000, 1, 1)
    resp = app.application.get_response('/logout')
    self.assertEquals('logins=; expires=2001-12-31 00:00:00; Path=/',
                      resp.headers['Set-Cookie'])
    self.assertEquals(302, resp.status_int)
    self.assertEquals('http://localhost/#!Logged%20out.', resp.headers['Location'])
