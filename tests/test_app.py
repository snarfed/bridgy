# coding=utf-8
"""Unit tests for app.py.
"""
from __future__ import unicode_literals
from __future__ import absolute_import
from future import standard_library
standard_library.install_aliases()
from builtins import object, str, zip
from future.utils import native_str

import datetime
import urllib.request, urllib.parse, urllib.error

from google.cloud import ndb
from mox3 import mox
from oauth_dropins.twitter import TwitterAuth
from oauth_dropins.webutil.util import json_dumps, json_loads
import tweepy

import app
import models
from models import Publish, PublishedPage, SyndicatedPost
import util
from . import testutil
from .testutil import FakeBlogSource
import twitter


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
    self.assert_multiline_equals(resp.body.decode('utf-8'), cached.html)

  def test_poll_now(self):
    self.assertEqual([], self.taskqueue_stub.GetTasks('poll'))

    key = self.sources[0].key.urlsafe()
    resp = app.application.get_response('/poll-now', method='POST',
                                        body=native_str(urllib.parse.urlencode({'key': key})))
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
      json_dumps({'object': {'url': 'https://fa.ke/1'}}),
      json_dumps({'url': 'https://fa.ke/2', 'object': {'unused': 'ok'}}),
      json_dumps({'url': 'https://fa.ke/3'}),
    ]
    resp.put()
    SyndicatedPost.insert(source, 'https://fa.ke/1', 'https://orig/1')
    SyndicatedPost.insert(source, 'https://fa.ke/2', 'http://orig/2')
    SyndicatedPost.insert(source, 'https://fa.ke/3', 'http://orig/3')

    # cached webmention endpoint
    util.webmention_endpoint_cache['W https skipped /'] = 'asdf'

    key = resp.key.urlsafe()
    response = app.application.get_response(
      '/retry', method='POST', body=native_str(urllib.parse.urlencode({'key': key})))
    self.assertEquals(302, response.status_int)
    self.assertEquals(source.bridgy_url(self.handler),
                      response.headers['Location'].split('#')[0])
    params = testutil.get_task_params(self.taskqueue_stub.GetTasks('propagate')[0])
    self.assertEqual(key, params['response_key'])

    # status and URLs should be refreshed
    got = resp.key.get()
    self.assertEqual('new', got.status)
    self.assertItemsEqual(
      ['http://unsent/', 'http://sent/', 'https://skipped/', 'http://error/',
       'http://failed/', 'https://orig/1', 'http://orig/2', 'http://orig/3'],
      got.unsent)
    for field in got.sent, got.skipped, got.error, got.failed:
      self.assertEqual([], field)

    # webmention endpoints for URL domains should be refreshed
    self.assertNotIn('W https skipped /', util.webmention_endpoint_cache)

    # shouldn't have refetched h-feed
    self.assertEqual(last_hfeed_refetch, source.key.get().last_hfeed_refetch)

  def test_retry_redirect_to(self):
    key = self.responses[0].put()
    response = app.application.get_response(
      '/retry', method='POST', body=native_str(urllib.parse.urlencode({
        'key': key.urlsafe(),
        'redirect_to': '/foo/bar',
      })))
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
      '/crawl-now', method='POST', body=native_str(urllib.parse.urlencode({'key': key})))
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
      for body in {}, {'key': self.responses[0].key.urlsafe()}:  # hasn't been stored
        resp = app.application.get_response(endpoint, method='POST',
                                            body=native_str(urllib.parse.urlencode(body)))
        self.assertEquals(400, resp.status_int)

  def test_delete_source_callback(self):
    auth_entity_key = self.sources[0].auth_entity.urlsafe()
    key = self.sources[0].key.urlsafe()

    resp = app.application.get_response(
      '/delete/start', method='POST', body=native_str(urllib.parse.urlencode({
        'feature': 'listen',
        'key': key,
        'callback': 'http://withknown.com/bridgy_callback',
      })))

    encoded_state = urllib.parse.quote_plus(json_dumps({
      'callback': 'http://withknown.com/bridgy_callback',
      'feature': 'listen',
      'operation': 'delete',
      'source': key,
    }, sort_keys=True))

    # when silo oauth is done, it should send us back to /SOURCE/delete/finish,
    # which would in turn redirect to the more general /delete/finish.
    expected_auth_url = 'http://fake/auth/url?' + urllib.parse.urlencode({
      'redirect_uri': 'http://localhost/fake/delete/finish?state='
      + encoded_state,
    })

    self.assertEquals(302, resp.status_int)
    self.assertEquals(expected_auth_url, resp.headers['Location'])

    # assume that the silo auth finishes and redirects to /delete/finish
    resp = app.application.get_response(native_str(
      '/delete/finish?'
      + 'auth_entity=' + auth_entity_key
      + '&state=' + encoded_state))

    self.assertEquals(302, resp.status_int)
    self.assertEquals(
      'http://withknown.com/bridgy_callback?' + urllib.parse.urlencode([
        ('result', 'success'),
        ('key', ndb.Key('FakeSource', '0123456789').urlsafe()),
        ('user', 'http://localhost/fake/0123456789')
      ]), resp.headers['Location'])

  def test_delete_source_declined(self):
    key = self.sources[0].key.urlsafe()
    resp = app.application.get_response(
      '/delete/start', method='POST', body=native_str(urllib.parse.urlencode({
        'feature': 'listen',
        'key': key,
        'callback': 'http://withknown.com/bridgy_callback',
      })))

    encoded_state = urllib.parse.quote_plus(json_dumps({
      'callback': 'http://withknown.com/bridgy_callback',
      'feature': 'listen',
      'operation': 'delete',
      'source': key,
    }, sort_keys=True))

    # when silo oauth is done, it should send us back to /SOURCE/delete/finish,
    # which would in turn redirect to the more general /delete/finish.
    expected_auth_url = 'http://fake/auth/url?' + urllib.parse.urlencode({
      'redirect_uri': 'http://localhost/fake/delete/finish?state='
      + encoded_state,
    })

    self.assertEquals(302, resp.status_int)
    self.assertEquals(expected_auth_url, resp.headers['Location'])

    # assume that the silo auth finishes
    resp = app.application.get_response(native_str(
      '/delete/finish?declined=True&state=' + encoded_state))

    self.assertEquals(302, resp.status_int)
    self.assertEquals(
      'http://withknown.com/bridgy_callback?' + urllib.parse.urlencode([
        ('result', 'declined')
      ]), resp.headers['Location'])

  def test_delete_start_redirect_url_error(self):
    self.mox.StubOutWithMock(testutil.OAuthStartHandler, 'redirect_url')
    testutil.OAuthStartHandler.redirect_url(state=mox.IgnoreArg()
      ).AndRaise(tweepy.TweepError('Connection closed unexpectedly...'))
    self.mox.ReplayAll()

    resp = app.application.get_response(
      '/delete/start', method='POST', body=native_str(urllib.parse.urlencode({
        'feature': 'listen',
        'key': self.sources[0].key.urlsafe(),
      })))
    self.assertEquals(302, resp.status_int)
    location = urllib.parse.urlparse(resp.headers['Location'])
    self.assertEquals('/fake/0123456789', location.path)
    self.assertEquals('!FakeSource API error 504: Connection closed unexpectedly...',
                      urllib.parse.unquote(location.fragment))

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
    self.sources[0].name = 'Snoøpy Barrett'
    self.sources[0].domains = ['foox.com']
    self.sources[0].put()

    for id in 'FooBar', 'Snoøpy Barrett', 'foox.com':
      resp = app.application.get_response(
        native_str('/fake/%s' % urllib.parse.quote(id.encode('utf-8'))))
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

    # test invite with missing object and content
    resp = json_loads(self.responses[8].response_json)
    resp['verb'] = 'invite'
    resp.pop('object', None)
    resp.pop('content', None)
    self.responses[8].response_json = json_dumps(resp)

    # test that invites render the invitee, not the inviter
    # https://github.com/snarfed/bridgy/issues/754
    self.responses[9].type = 'rsvp'
    self.responses[9].response_json = json_dumps({
      'id': 'tag:fa.ke,2013:111',
      'objectType': 'activity',
      'verb': 'invite',
      'url': 'http://fa.ke/event',
      'actor': {
        'displayName': 'Mrs. Host',
        'url': 'http://fa.ke/host',
      },
      'object': {
        'objectType': 'person',
        'displayName': 'Ms. Guest',
        'url': 'http://fa.ke/guest',
      },
    })

    for entity in self.responses + self.publishes + self.blogposts:
      entity.put()

    user_url = self.sources[0].bridgy_path()
    response = app.application.get_response(user_url)
    self.assertEquals(200, response.status_int)

    parsed = util.parse_mf2(response.body, user_url)
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
      self.assertEquals([json_loads(resp.activities_json[0])['url']],
                        props['bridgy-original-source'])
      self.assertEquals(resp.unsent, props['bridgy-target'])

    # check invite
    html = response.body.decode('utf-8')
    self.assertIn('Ms. Guest is invited.', html)
    self.assertNotIn('Mrs. Host is invited.', html)

    publish = hcard['children'][len(expected_resps)]
    self.assertIn('h-bridgy-publish', publish['type'])
    props = publish['properties']
    self.assertEquals([self.publishes[0].key.parent().id()], props['url'])
    self.assertEquals([self.publishes[0].status], props['bridgy-status'])

  def test_user_page_private_twitter(self):
    auth_entity = TwitterAuth(
      id='foo',
      user_json=json_dumps({'protected': True}),
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

    url = 'https://ptt.com/ransomw…ocks-user-access/'
    Publish(parent=PublishedPage(id=url.encode('utf-8')).key,
            source=self.sources[0].key).put()

    user_url = self.sources[0].bridgy_path()
    resp = app.application.get_response(user_url)
    self.assertEquals(200, resp.status_int)

    parsed = util.parse_mf2(resp.body, user_url)
    publish = parsed['items'][0]['children'][0]

  def test_user_page_escapes_html_chars(self):
    html = '<xyz> a&b'
    escaped = '&lt;xyz&gt; a&amp;b'

    activity = json_loads(self.responses[0].activities_json[0])
    activity['object']['content'] = escaped
    self.responses[0].activities_json = [json_dumps(activity)]

    resp = json_loads(self.responses[0].response_json)
    resp['content'] = escaped
    self.responses[0].response_json = json_dumps(resp)
    self.responses[0].status = 'processing'
    self.responses[0].put()

    resp = app.application.get_response(self.sources[0].bridgy_path())
    self.assertEquals(200, resp.status_int)
    self.assertNotIn(html, resp.body)
    self.assertIn(escaped, resp.body)

    self.assertNotIn('&lt;span class="glyphicon glyphicon-transfer"&gt;', resp.body)
    self.assertIn('<span class="glyphicon glyphicon-transfer">', resp.body)

  def test_user_page_rate_limited_never_successfully_polled(self):
    self.sources[0].rate_limited = True
    self.sources[0].last_poll_attempt = datetime.datetime(2019, 1, 1)
    self.sources[0].put()

    resp = app.application.get_response(self.sources[0].bridgy_path())
    self.assertEquals(200, resp.status_int)
    self.assertIn('Not polled yet,', resp.body.decode('utf-8'))

  def test_blog_user_page_escapes_html_chars(self):
    html = '<xyz> a&b'
    escaped = '&lt;xyz&gt; a&amp;b'

    # self.mox.StubOutWithMock(FakeSource, 'template_file')
    # FakeSource.template_file(mox.IgnoreArg()).AndReturn('blog_user.html')
    # self.mox.ReplayAll()

    source = FakeBlogSource.new(None)#, auth_entity=self.auth_entities[0])
    source.features = ['webmention']
    source.put()

    self.blogposts[0].source = source.key
    self.blogposts[0].feed_item['title'] = html
    self.blogposts[0].put()

    resp = app.application.get_response(source.bridgy_path())
    self.assertEquals(200, resp.status_int)
    self.assertNotIn(html, resp.body)
    self.assertIn(escaped, resp.body)

  def test_users_page(self):
    resp = app.application.get_response('/users')
    for source in self.sources:
      self.assertIn(
        '<a href="%s" title="%s"' % (source.bridgy_path(), source.label()),
        resp.body)
    self.assertEquals(200, resp.status_int)

  def test_users_page_hides_deleted_and_disabled(self):
    deleted = testutil.FakeSource.new(None, features=[])
    deleted.put()
    disabled = testutil.FakeSource.new(None, status='disabled', features=['publish'])
    disabled.put()

    resp = app.application.get_response('/users')
    for entity in deleted, disabled:
      self.assertNotIn(
        '<a href="%s" title="%s"' % (entity.bridgy_path(), entity.label()),
        resp.body)

  def test_logout(self):
    util.now_fn = lambda: datetime.datetime(2000, 1, 1)
    resp = app.application.get_response('/logout')
    self.assertEquals('logins=; expires=2001-12-31 00:00:00; Path=/',
                      resp.headers['Set-Cookie'])
    self.assertEquals(302, resp.status_int)
    self.assertEquals('http://localhost/#!Logged%20out.', resp.headers['Location'])

  def test_edit_web_sites_add(self):
    source = self.sources[0]
    self.assertNotIn('foo.com', source.domains)
    resp = app.application.get_response('/edit-websites', method='POST',
                                        body=native_str(urllib.parse.urlencode({
      'source_key': source.key.urlsafe(),
      'add': 'http://foo.com/',
    })))
    self.assertEquals(302, resp.status_int)
    self.assertEquals('http://localhost/edit-websites?source_key=%s#!%s' % (
      (source.key.urlsafe(),
       urllib.parse.quote('Added <a href="http://foo.com/">foo.com</a>.'))),
      resp.headers['Location'])

    source = source.key.get()
    self.assertIn('foo.com', source.domains)
    self.assertIn('http://foo.com/', source.domain_urls)

  def test_edit_web_sites_add_existing(self):
    source = self.sources[0]
    source.domain_urls = ['http://foo.com/']
    source.domains = ['foo.com']
    source.put()

    resp = app.application.get_response('/edit-websites', method='POST',
                                        body=native_str(urllib.parse.urlencode({
      'source_key': source.key.urlsafe(),
      'add': 'http://foo.com/',
    })))
    self.assertEquals(302, resp.status_int)
    self.assertEquals('http://localhost/edit-websites?source_key=%s#!%s' % (
      (source.key.urlsafe(),
       urllib.parse.quote('<a href="http://foo.com/">foo.com</a> already exists.'))),
      resp.headers['Location'])

    source = source.key.get()
    self.assertEquals(['foo.com'], source.domains)
    self.assertEquals(['http://foo.com/'], source.domain_urls)

  def test_edit_web_sites_add_bad(self):
    source = self.sources[0]
    resp = app.application.get_response('/edit-websites', method='POST',
                                        body=native_str(urllib.parse.urlencode({
      'source_key': source.key.urlsafe(),
      'add': 'http://facebook.com/',
    })))
    self.assertEquals(302, resp.status_int)
    self.assertEquals('http://localhost/edit-websites?source_key=%s#!%s' % (
      (source.key.urlsafe(),
       urllib.parse.quote('<a href="http://facebook.com/">facebook.com</a> doesn\'t look like your web site. Try again?'))),
      resp.headers['Location'])

    source = source.key.get()
    self.assertEquals([], source.domains)
    self.assertEquals([], source.domain_urls)

  def test_edit_web_sites_delete(self):
    source = self.sources[0]
    source.domain_urls = ['http://foo/', 'https://bar']
    source.domains = ['foo', 'bar']
    source.put()

    resp = app.application.get_response('/edit-websites', method='POST',
                                        body=native_str(urllib.parse.urlencode({
      'source_key': source.key.urlsafe(),
      'delete': 'https://bar',
    })))
    self.assertEquals(302, resp.status_int)
    self.assertEquals('http://localhost/edit-websites?source_key=%s#!%s' % (
      (source.key.urlsafe(),
       urllib.parse.quote('Removed <a href="https://bar">bar</a>.'))),
      resp.headers['Location'])

    source = source.key.get()
    self.assertEquals(['foo'], source.domains)
    self.assertEquals(['http://foo/'], source.domain_urls)

  def test_edit_web_sites_delete_multiple_urls_same_domain(self):
    source = self.sources[0]
    source.domain_urls = ['http://foo.com/bar', 'https://foo.com/baz']
    source.domains = ['foo.com']
    source.put()

    resp = app.application.get_response('/edit-websites', method='POST',
                                        body=native_str(urllib.parse.urlencode({
      'source_key': source.key.urlsafe(),
      'delete': 'https://foo.com/baz',
    })))
    self.assertEquals(302, resp.status_int)
    self.assertEquals('http://localhost/edit-websites?source_key=%s#!%s' % (
      (source.key.urlsafe(),
       urllib.parse.quote('Removed <a href="https://foo.com/baz">foo.com/baz</a>.'))),
      resp.headers['Location'])

    source = source.key.get()
    self.assertEquals(['foo.com'], source.domains)
    self.assertEquals(['http://foo.com/bar'], source.domain_urls)

  def test_edit_web_sites_errors(self):
    source_key = self.sources[0].key.urlsafe()
    for data in (
        {},
        {'source_key': source_key},
        {'add': 'http://foo'},
        {'delete': 'http://foo'},
        {'source_key': 'asdf', 'add': 'http://foo'},
        {'source_key': 'asdf', 'delete': 'http://foo', 'add': 'http://bar'},
        {'source_key': source_key, 'delete': 'http://missing'},
    ):
      resp = app.application.get_response('/edit-websites', method='POST',
                                          body=native_str(urllib.parse.urlencode(data)))
      self.assertEquals(400, resp.status_int)


class DiscoverTest(testutil.ModelsTest):

  def setUp(self):
    super(DiscoverTest, self).setUp()
    self.source = self.sources[0]
    self.source.domains = ['si.te']
    self.source.put()

  def check_discover(self, url, expected_message):
      resp = app.application.get_response(
        '/discover?source_key=%s&url=%s' % (self.source.key.urlsafe(), url),
        method='POST')
      location = urllib.parse.urlparse(resp.headers['Location'])
      detail = ' '.join((url, str(resp.status_int), repr(location), repr(resp.body)))
      self.assertEquals(302, resp.status_int, detail)
      self.assertEqual(self.source.bridgy_path(), location.path, detail)
      self.assertEqual('!' + expected_message, urllib.parse.unquote(location.fragment),
                       detail)

  def check_fail(self, body, **kwargs):
    self.expect_requests_get('http://si.te/123', body, **kwargs)
    self.mox.ReplayAll()

    self.check_discover('http://si.te/123',
        'Failed to fetch <a href="http://si.te/123">si.te/123</a> or '
        'find a FakeSource syndication link.')
    self.assertEqual([], self.taskqueue_stub.GetTasks('discover'))

  def test_discover_param_errors(self):
    for url in ('/discover',
                '/discover?key=bad',
                '/discover?key=%s' % self.source.key,
                '/discover?url=bad',
                '/discover?url=http://foo/bar',
                ):
      resp = app.application.get_response(url, method='POST')
      self.assertEquals(400, resp.status_int)
      self.assertEqual([], self.taskqueue_stub.GetTasks('discover'))

  def test_discover_url_not_site_or_silo_error(self):
    msg = 'Please enter a URL on either your web site or FakeSource.'
    for url in ('http://not/site/or/silo',): # 'http://fa.ke/not/a/post':
      self.check_discover(url, msg)
      self.assertEqual([], self.taskqueue_stub.GetTasks('discover'))

  def test_discover_url_silo_post(self):
    self.check_discover('http://fa.ke/123',
        'Discovering now. Refresh in a minute to see the results!')

    tasks = self.taskqueue_stub.GetTasks('discover')
    self.assertEqual(1, len(tasks))
    self.assertEqual({
      'source_key': self.source.key.urlsafe(),
      'post_id': '123',
    }, testutil.get_task_params(tasks[0]))

  def test_discover_url_silo_event(self):
    self.check_discover('http://fa.ke/events/123',
        'Discovering now. Refresh in a minute to see the results!')

    tasks = self.taskqueue_stub.GetTasks('discover')
    self.assertEqual(1, len(tasks))
    self.assertEqual({
      'source_key': self.source.key.urlsafe(),
      'post_id': '123',
      'type': 'event',
    }, testutil.get_task_params(tasks[0]))

  def test_discover_url_silo_not_post_url(self):
    self.check_discover('http://fa.ke/',
        "Sorry, that doesn't look like a FakeSource post URL.")
    self.assertEqual(0, len(self.taskqueue_stub.GetTasks('discover')))

  def test_discover_twitter_profile_url_error(self):
    """https://console.cloud.google.com/errors/7553065641439031622"""
    self.source = twitter.Twitter(id='bltavares', features=['listen'])
    self.source.put()
    self.check_discover('https://twitter.com/bltavares',
        "Sorry, that doesn't look like a Twitter post URL.")

  def test_discover_url_site_post_fetch_fails(self):
    self.check_fail('fooey', status_code=404)

  def test_discover_url_site_post_no_mf2(self):
    self.check_fail('<html><body>foo</body></html>')

  def test_discover_url_site_post_no_hentry(self):
    self.check_fail('<html><body><div class="h-card">foo</div></body></html>')

  def test_discover_url_site_post_no_syndication_links(self):
    self.check_fail('<html><body><div class="h-entry">foo</div></body></html>')

  def test_discover_url_site_post_syndication_link_to_other_silo(self):
    self.check_fail("""
<div class="h-entry">
  foo <a class="u-syndication" href="http://other/silo"></a>
</div>""")

  def test_discover_url_site_post_syndication_links(self):
    self.expect_requests_get('http://si.te/123', """
<div class="h-entry">
  foo
  <a class="u-syndication" href="http://fa.ke/222"></a>
  <a class="u-syndication" href="http://other/silo"></a>
  <a class="u-syndication" href="http://fa.ke/post/444"></a>
</div>""")
    self.mox.ReplayAll()

    self.assertEqual(0, SyndicatedPost.query().count())
    self.check_discover('http://si.te/123',
        'Discovering now. Refresh in a minute to see the results!')

    self.assertItemsEqual([
      {'https://fa.ke/222': 'http://si.te/123'},
      {'https://fa.ke/post/444': 'http://si.te/123'},
      ], [{sp.syndication: sp.original} for sp in models.SyndicatedPost.query()])

    tasks = self.taskqueue_stub.GetTasks('discover')
    key = self.source.key.urlsafe()
    self.assertEqual([
      {'source_key': key, 'post_id': '222'},
      {'source_key': key, 'post_id': '444'},
    ], [testutil.get_task_params(task) for task in tasks])

    now = util.now_fn()
    source = self.source.key.get()
    self.assertEqual(now, source.last_syndication_url)

  def test_discover_url_site_post_last_feed_syndication_url(self):
    now = util.now_fn()
    self.source.last_feed_syndication_url = now
    self.source.put()

    self.expect_requests_get('http://si.te/123', """
<div class="h-entry">
  <a class="u-syndication" href="http://fa.ke/222"></a>
</div>""")
    self.mox.ReplayAll()

    self.check_discover('http://si.te/123',
        'Discovering now. Refresh in a minute to see the results!')

    tasks = self.taskqueue_stub.GetTasks('discover')
    key = self.source.key.urlsafe()
    self.assertEqual([{'source_key': key, 'post_id': '222'}],
                     [testutil.get_task_params(task) for task in tasks])

    source = self.source.key.get()
    self.assertEqual(now, source.last_syndication_url)
