# coding=utf-8
"""Unit tests for app.py."""
import datetime
import urllib.request, urllib.parse, urllib.error
from urllib.parse import urlencode

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


class AppTest(testutil.ModelsTest, testutil.ViewTest):

  def test_front_page(self):
    resp = self.client.get('/')
    self.assertEqual(200, resp.status_code)

  def test_poll_now(self):
    key = self.sources[0].key.urlsafe().decode()
    self.expect_task('poll-now', source_key=key, last_polled='1970-01-01-00-00-00')
    self.mox.ReplayAll()

    resp = self.client.post('/poll-now', data={'key': key})
    self.assertEqual(302, resp.status_code)
    self.assertEqual(self.sources[0].bridgy_url(),
                      resp.headers['Location'].split('#')[0])

  def test_retry(self):
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

    key = resp.key.urlsafe().decode()
    self.expect_task('propagate', response_key=key)
    self.mox.ReplayAll()

    # cached webmention endpoint
    util.webmention_endpoint_cache['W https skipped /'] = 'asdf'

    response = self.client.post('/retry', data={'key': key})
    self.assertEqual(302, response.status_code)
    self.assertEqual(source.bridgy_url(),
                     response.headers['Location'].split('#')[0])

    # status and URLs should be refreshed
    got = resp.key.get()
    self.assertEqual('new', got.status)
    self.assertCountEqual(
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
    key = self.responses[0].put().urlsafe().decode()
    self.expect_task('propagate', response_key=key)
    self.mox.ReplayAll()

    response = self.client.post('/retry', data={
        'key': key,
        'redirect_to': '/foo/bar',
      })
    self.assertEqual(302, response.status_code)
    self.assertEqual('http://localhost/foo/bar',
                      response.headers['Location'].split('#')[0])

  def test_crawl_now(self):
    source = self.sources[0]
    source.domain_urls = ['http://orig']
    source.last_hfeed_refetch = source.last_feed_syndication_url = testutil.NOW
    source.put()

    key = source.key.urlsafe().decode()
    self.expect_task('poll-now', source_key=key, last_polled='1970-01-01-00-00-00')
    self.mox.ReplayAll()

    response = self.client.post('/crawl-now', data={'key': key})
    self.assertEqual(302, response.status_code)
    self.assertEqual(source.bridgy_url(),
                     response.headers['Location'].split('#')[0])

    source = source.key.get()
    self.assertEqual(models.REFETCH_HFEED_TRIGGER, source.last_hfeed_refetch)
    self.assertIsNone(source.last_feed_syndication_url)

  def test_poll_now_and_retry_response_missing_key(self):
    for endpoint in '/poll-now', '/retry':
      for body in {}, {'key': self.responses[0].key.urlsafe().decode()}:  # hasn't been stored
        resp = self.client.post(endpoint, data=body)
        self.assertEqual(400, resp.status_code)

  def test_delete_source_callback(self):
    key = self.sources[0].key.urlsafe().decode()

    resp = self.client.post('/delete/start', data={
        'feature': 'listen',
        'key': key,
        'callback': 'http://withknown.com/bridgy_callback',
      })

    encoded_state = urllib.parse.quote_plus(json_dumps({
      'callback': 'http://withknown.com/bridgy_callback',
      'feature': 'listen',
      'operation': 'delete',
      'source': key,
    }, sort_keys=True))

    # when silo oauth is done, it should send us back to /SOURCE/delete/finish,
    # which would in turn redirect to the more general /delete/finish.
    expected_auth_url = 'http://fake/auth/url?' + urlencode({
      'redirect_uri': 'http://localhost/fake/delete/finish?state='
      + encoded_state,
    })

    self.assertEqual(302, resp.status_code)
    self.assertEqual(expected_auth_url, resp.headers['Location'])

    # assume that the silo auth finishes and redirects to /delete/finish
    resp = self.client.get(
      '/delete/finish?'
      + 'auth_entity=' + self.sources[0].auth_entity.urlsafe().decode()
      + '&state=' + encoded_state)

    self.assertEqual(302, resp.status_code)
    self.assertEqual(
      'http://withknown.com/bridgy_callback?' + urlencode([
        ('result', 'success'),
        ('user', 'http://localhost/fake/0123456789'),
        ('key', ndb.Key('FakeSource', '0123456789').urlsafe().decode()),
      ]), resp.headers['Location'])

  def test_delete_source_declined(self):
    key = self.sources[0].key.urlsafe().decode()
    resp = self.client.post('/delete/start', data={
        'feature': 'listen',
        'key': key,
        'callback': 'http://withknown.com/bridgy_callback',
      })

    encoded_state = urllib.parse.quote_plus(json_dumps({
      'callback': 'http://withknown.com/bridgy_callback',
      'feature': 'listen',
      'operation': 'delete',
      'source': key,
    }, sort_keys=True))

    # when silo oauth is done, it should send us back to /SOURCE/delete/finish,
    # which would in turn redirect to the more general /delete/finish.
    expected_auth_url = 'http://fake/auth/url?' + urlencode({
      'redirect_uri': 'http://localhost/fake/delete/finish?state='
      + encoded_state,
    })

    self.assertEqual(302, resp.status_code)
    self.assertEqual(expected_auth_url, resp.headers['Location'])

    # assume that the silo auth finishes
    resp = self.client.get(
      '/delete/finish?declined=True&state=' + encoded_state)

    self.assertEqual(302, resp.status_code)
    self.assertEqual(
      'http://withknown.com/bridgy_callback?' + urlencode([
        ('result', 'declined')
      ]), resp.headers['Location'])

  def test_delete_start_redirect_url_error(self):
    self.mox.StubOutWithMock(testutil.OAuthStart, 'redirect_url')
    testutil.OAuthStart.redirect_url(state=mox.IgnoreArg()
      ).AndRaise(tweepy.TweepError('Connection closed unexpectedly...'))
    self.mox.ReplayAll()

    resp = self.client.post('/delete/start', data={
        'feature': 'listen',
        'key': self.sources[0].key.urlsafe().decode(),
      })
    self.assertEqual(302, resp.status_code)
    location = urllib.parse.urlparse(resp.headers['Location'])
    self.assertEqual('/fake/0123456789', location.path)
    self.assertEqual('!FakeSource API error 504: Connection closed unexpectedly...',
                      urllib.parse.unquote(location.fragment))

  def test_delete_removes_from_logins_cookie(self):
    cookie = ('logins="/fake/%s?Fake%%20User|/other/1?bob"; '
              'expires="2999-12-31 00:00:00"; Path=/' % self.sources[0].key.id())

    with app.app.test_request_context():
      state = util.construct_state_param_for_add(
        feature='listen', operation='delete',
        source=self.sources[0].key.urlsafe().decode())

    resp = self.client.get(
      '/delete/finish?auth_entity=%s&state=%s' %
      (self.sources[0].auth_entity.urlsafe().decode(), state),
      headers={'Cookie': cookie})

    self.assertEqual(302, resp.status_code)
    location = resp.headers['Location']
    self.assertTrue(location.startswith('http://localhost/#'), location)
    new_cookie = resp.headers['Set-Cookie']
    self.assertTrue(new_cookie.startswith('logins="/other/1?bob"; '), new_cookie)

  def test_user_page(self):
    resp = self.client.get(self.sources[0].bridgy_path())
    self.assertEqual(200, resp.status_code)

  def test_user_page_lookup_with_username_etc(self):
    self.sources[0].username = 'FooBar'
    self.sources[0].name = 'Snoøpy Barrett'
    self.sources[0].domains = ['foox.com']
    self.sources[0].put()

    for id in 'FooBar', 'Snoøpy Barrett', 'foox.com':
      resp = self.client.get(
        '/fake/%s' % urllib.parse.quote(id.encode()))
      self.assertEqual(301, resp.status_code)
      self.assertEqual('http://localhost/fake/%s' % self.sources[0].key.id(),
                        resp.headers['Location'])

    resp = self.client.get('/fake/nope')
    self.assertEqual(404, resp.status_code)

  def test_user_page_with_no_features_404s(self):
    self.sources[0].features = []
    self.sources[0].put()

    resp = self.client.get(self.sources[0].bridgy_path())
    self.assertEqual(404, resp.status_code)

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
    response = self.client.get(user_url)
    self.assertEqual(200, response.status_code)

    parsed = util.parse_mf2(response.get_data(as_text=True), user_url)
    hcard = parsed.get('items', [])[0]
    self.assertEqual(['h-card'], hcard['type'])
    self.assertEqual(
      ['Fake User'], hcard['properties'].get('name'))
    self.assertEqual(
      ['http://fa.ke/profile/url'], hcard['properties'].get('url'))
    self.assertEqual(
      ['enabled'], hcard['properties'].get('bridgy-account-status'))
    self.assertEqual(
      ['enabled'], hcard['properties'].get('bridgy-listen-status'))
    self.assertEqual(
      ['enabled'], hcard['properties'].get('bridgy-publish-status'))

    expected_resps = self.responses[:10]
    for item, resp in zip(hcard['children'], expected_resps):
      self.assertIn('h-bridgy-response', item['type'])
      props = item['properties']
      self.assertEqual([resp.status], props['bridgy-status'])
      self.assertEqual([json_loads(resp.activities_json[0])['url']],
                        props['bridgy-original-source'])
      self.assertEqual(resp.unsent, props['bridgy-target'])

    # check invite
    self.assertIn('Ms. Guest is invited.', response.get_data(as_text=True))
    self.assertNotIn('Mrs. Host is invited.', response.get_data(as_text=True))

    publish = hcard['children'][len(expected_resps)]
    self.assertIn('h-bridgy-publish', publish['type'])
    props = publish['properties']
    self.assertEqual([self.publishes[0].key.parent().id()], props['url'])
    self.assertEqual([self.publishes[0].status], props['bridgy-status'])

  def test_user_page_private_twitter(self):
    auth_entity = TwitterAuth(
      id='foo',
      user_json=json_dumps({'protected': True}),
      token_key='', token_secret='',
    ).put()
    tw = twitter.Twitter(id='foo', auth_entity=auth_entity, features=['listen'])
    tw.put()

    resp = self.client.get(tw.bridgy_path())
    self.assertEqual(200, resp.status_code)
    self.assertIn('Your Twitter account is private!', resp.get_data(as_text=True))
    self.assertNotIn('most of your recent posts are private', resp.get_data(as_text=True))

  def test_user_page_recent_private_posts(self):
    self.sources[0].recent_private_posts = app.RECENT_PRIVATE_POSTS_THRESHOLD
    self.sources[0].put()

    resp = self.client.get(self.sources[0].bridgy_path())
    self.assertEqual(200, resp.status_code)
    self.assertIn('most of your recent posts are private', resp.get_data(as_text=True))

  def test_user_page_recent_private_posts_none(self):
    self.sources[0].recent_private_posts = None
    self.sources[0].put()

    resp = self.client.get(self.sources[0].bridgy_path())
    self.assertEqual(200, resp.status_code)
    self.assertNotIn('most of your recent posts are private', resp.get_data(as_text=True))

  def test_user_page_publish_url_with_unicode_char(self):
    """Check the custom mf2 we render on social user pages."""
    self.sources[0].features = ['publish']
    self.sources[0].put()

    url = 'https://ptt.com/ransomw…ocks-user-access/'
    Publish(parent=PublishedPage(id=url).key,
            source=self.sources[0].key).put()

    user_url = self.sources[0].bridgy_path()
    resp = self.client.get(user_url)
    self.assertEqual(200, resp.status_code)

    parsed = util.parse_mf2(resp.get_data(as_text=True), user_url)
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

    resp = self.client.get(self.sources[0].bridgy_path())
    self.assertEqual(200, resp.status_code)
    self.assertNotIn(html, resp.get_data(as_text=True))
    self.assertIn(escaped, resp.get_data(as_text=True))

    self.assertNotIn('&lt;span class="glyphicon glyphicon-transfer"&gt;', resp.get_data(as_text=True))
    self.assertIn('<span class="glyphicon glyphicon-transfer">', resp.get_data(as_text=True))

  def test_user_page_rate_limited_never_successfully_polled(self):
    self.sources[0].rate_limited = True
    self.sources[0].last_poll_attempt = datetime.datetime(2019, 1, 1)
    self.sources[0].put()

    resp = self.client.get(self.sources[0].bridgy_path())
    self.assertEqual(200, resp.status_code)
    self.assertIn('Not polled yet,', resp.get_data(as_text=True))

  def test_blog_user_page_escapes_html_chars(self):
    html = '<xyz> a&b'
    escaped = '&lt;xyz&gt; a&amp;b'

    source = FakeBlogSource.new()
    source.features = ['webmention']
    source.put()

    self.blogposts[0].source = source.key
    self.blogposts[0].feed_item['title'] = html
    self.blogposts[0].put()

    resp = self.client.get(source.bridgy_path())
    self.assertEqual(200, resp.status_code)
    self.assertNotIn(html, resp.get_data(as_text=True))
    self.assertIn(escaped, resp.get_data(as_text=True))

  def test_users_page(self):
    resp = self.client.get('/users')
    for source in self.sources:
      self.assertIn(
        '<a href="%s" title="%s"' % (source.bridgy_path(), source.label()),
        resp.get_data(as_text=True))
    self.assertEqual(200, resp.status_code)

  def test_users_page_hides_deleted_and_disabled(self):
    deleted = testutil.FakeSource.new(features=[])
    deleted.put()
    disabled = testutil.FakeSource.new(status='disabled', features=['publish'])
    disabled.put()

    resp = self.client.get('/users')
    for entity in deleted, disabled:
      self.assertNotIn(
        '<a href="%s" title="%s"' % (entity.bridgy_path(), entity.label()),
        resp.get_data(as_text=True))

  def test_logout(self):
    util.now_fn = lambda: datetime.datetime(2000, 1, 1)
    resp = self.client.get('/logout')
    self.assertEqual('logins=""; expires="2001-12-31 00:00:00"; Path=/',
                     resp.headers['Set-Cookie'])
    self.assertEqual(302, resp.status_code)
    self.assertEqual('http://localhost/#!Logged%20out.', resp.headers['Location'])

  def test_edit_web_sites_add(self):
    source = self.sources[0]
    self.assertNotIn('foo.com', source.domains)
    resp = self.client.post('/edit-websites', data={
      'source_key': source.key.urlsafe().decode(),
      'add': 'http://foo.com/',
    })
    self.assertEqual(302, resp.status_code)
    self.assertEqual('http://localhost/edit-websites?source_key=%s#!%s' % (
      (source.key.urlsafe().decode(),
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

    resp = self.client.post('/edit-websites', data={
      'source_key': source.key.urlsafe().decode(),
      'add': 'http://foo.com/',
    })
    self.assertEqual(302, resp.status_code)
    self.assertEqual('http://localhost/edit-websites?source_key=%s#!%s' % (
      (source.key.urlsafe().decode(),
       urllib.parse.quote('<a href="http://foo.com/">foo.com</a> already exists.'))),
      resp.headers['Location'])

    source = source.key.get()
    self.assertEqual(['foo.com'], source.domains)
    self.assertEqual(['http://foo.com/'], source.domain_urls)

  def test_edit_web_sites_add_bad(self):
    source = self.sources[0]
    resp = self.client.post('/edit-websites', data={
      'source_key': source.key.urlsafe().decode(),
      'add': 'http://facebook.com/',
    })
    self.assertEqual(302, resp.status_code)
    self.assertEqual('http://localhost/edit-websites?source_key=%s#!%s' % (
      (source.key.urlsafe().decode(),
       urllib.parse.quote('<a href="http://facebook.com/">facebook.com</a> doesn\'t look like your web site. Try again?'))),
      resp.headers['Location'])

    source = source.key.get()
    self.assertEqual([], source.domains)
    self.assertEqual([], source.domain_urls)

  def test_edit_web_sites_delete(self):
    source = self.sources[0]
    source.domain_urls = ['http://foo/', 'https://bar']
    source.domains = ['foo', 'bar']
    source.put()

    resp = self.client.post('/edit-websites', data={
      'source_key': source.key.urlsafe().decode(),
      'delete': 'https://bar',
    })
    self.assertEqual(302, resp.status_code)
    self.assertEqual('http://localhost/edit-websites?source_key=%s#!%s' % (
      (source.key.urlsafe().decode(),
       urllib.parse.quote('Removed <a href="https://bar">bar</a>.'))),
      resp.headers['Location'])

    source = source.key.get()
    self.assertEqual(['foo'], source.domains)
    self.assertEqual(['http://foo/'], source.domain_urls)

  def test_edit_web_sites_delete_multiple_urls_same_domain(self):
    source = self.sources[0]
    source.domain_urls = ['http://foo.com/bar', 'https://foo.com/baz']
    source.domains = ['foo.com']
    source.put()

    resp = self.client.post('/edit-websites', data={
      'source_key': source.key.urlsafe().decode(),
      'delete': 'https://foo.com/baz',
    })
    self.assertEqual(302, resp.status_code)
    self.assertEqual('http://localhost/edit-websites?source_key=%s#!%s' % (
      (source.key.urlsafe().decode(),
       urllib.parse.quote('Removed <a href="https://foo.com/baz">foo.com/baz</a>.'))),
      resp.headers['Location'])

    source = source.key.get()
    self.assertEqual(['foo.com'], source.domains)
    self.assertEqual(['http://foo.com/bar'], source.domain_urls)

  def test_edit_web_sites_errors(self):
    source_key = self.sources[0].key.urlsafe().decode()

    for data in (
        {},
        {'source_key': source_key},
        {'add': 'http://foo'},
        {'delete': 'http://foo'},
        {'source_key': 'asdf', 'add': 'http://foo'},
        {'source_key': 'asdf', 'delete': 'http://foo', 'add': 'http://bar'},
        {'source_key': source_key, 'delete': 'http://missing'},
    ):
      resp = self.client.post('/edit-websites', data=data)
      self.assertEqual(400, resp.status_code)


class DiscoverTest(testutil.ModelsTest, testutil.ViewTest):

  def setUp(self):
    super().setUp()
    self.source = self.sources[0]
    self.source.domains = ['si.te']
    self.source.put()

  def check_discover(self, url, expected_message):
      resp = self.client.get('/discover', data={
        'source_key': self.source.key.urlsafe().decode(),
        'url': url,
      })
      location = urllib.parse.urlparse(resp.headers['Location'])
      detail = ' '.join((url, str(resp.status_code), repr(location), repr(resp.get_data(as_text=True))))
      self.assertEqual(302, resp.status_code, detail)
      self.assertEqual(self.source.bridgy_path(), location.path, detail)
      self.assertEqual('!' + expected_message, urllib.parse.unquote(location.fragment),
                       detail)

  def check_fail(self, body, **kwargs):
    self.expect_requests_get('http://si.te/123', body, **kwargs)
    self.mox.ReplayAll()

    self.check_discover('http://si.te/123',
        'Failed to fetch <a href="http://si.te/123">si.te/123</a> or '
        'find a FakeSource syndication link.')

    # tasks_client.create_task() is stubbed out, so if any calls to it were
    # made, mox would notice that and fail.

  def test_discover_param_errors(self):
    for url in ('/discover',
                '/discover?key=bad',
                '/discover?key=%s' % self.source.key,
                '/discover?url=bad',
                '/discover?url=http://foo/bar',
                ):
      resp = self.client.post(url)
      self.assertEqual(400, resp.status_code)

  def test_discover_url_not_site_or_silo_error(self):
    self.check_discover('http://not/site/or/silo',
                        'Please enter a URL on either your web site or FakeSource.')

  def test_discover_url_silo_post(self):
    self.expect_task('discover', source_key=self.source, post_id='123')
    self.mox.ReplayAll()

    self.check_discover('http://fa.ke/123',
        'Discovering now. Refresh in a minute to see the results!')

  def test_discover_url_silo_event(self):
    self.expect_task('discover', source_key=self.source, post_id='123',
                     type='event')
    self.mox.ReplayAll()

    self.check_discover('http://fa.ke/events/123',
                        'Discovering now. Refresh in a minute to see the results!')

  def test_discover_url_silo_not_post_url(self):
    self.check_discover('http://fa.ke/',
        "Sorry, that doesn't look like a FakeSource post URL.")

  def test_discover_twitter_profile_url_error(self):
    """https://console.cloud.google.com/errors/7553065641439031622"""
    auth_entity = TwitterAuth(id='foo', user_json='',
                              token_key='', token_secret='').put()
    self.source = twitter.Twitter(id='foo', features=['listen'],
                                  auth_entity=auth_entity)
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

    self.expect_task('discover', source_key=self.source, post_id='222')
    self.expect_task('discover', source_key=self.source, post_id='444')
    self.mox.ReplayAll()

    self.assertEqual(0, SyndicatedPost.query().count())
    self.check_discover('http://si.te/123',
        'Discovering now. Refresh in a minute to see the results!')

    self.assertCountEqual([
      {'https://fa.ke/222': 'http://si.te/123'},
      {'https://fa.ke/post/444': 'http://si.te/123'},
      ], [{sp.syndication: sp.original} for sp in models.SyndicatedPost.query()])

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

    self.expect_task('discover', source_key=self.source, post_id='222')
    self.mox.ReplayAll()

    self.check_discover('http://si.te/123',
        'Discovering now. Refresh in a minute to see the results!')

    source = self.source.key.get()
    self.assertEqual(now, source.last_syndication_url)
