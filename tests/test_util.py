# coding=utf-8
"""Unit tests for util.py."""
from datetime import datetime, timezone
import time
import urllib.request, urllib.parse, urllib.error

from flask import Flask, get_flashed_messages, request
from flask.views import View
from google.cloud import ndb
from oauth_dropins import views as oauth_views
from oauth_dropins.webutil.util import json_dumps, json_loads
import requests
from werkzeug.exceptions import BadRequest
from werkzeug.routing import RequestRedirect

from flask_app import app
from models import Source
from . import testutil
from .testutil import FakeAuthEntity, FakeGrSource, FakeSource
from twitter import Twitter
import util
from util import Login

# the character in the middle is an unusual unicode character
UNICODE_STR = 'a ✁ b'


class UtilTest(testutil.AppTest):

  def setUp(self):
    super().setUp()
    util.now_fn = lambda: datetime(2000, 1, 1, tzinfo=timezone.utc)

  def test_maybe_add_or_delete_source(self):
    auth_entity = FakeAuthEntity(id='x', user_json=json_dumps(
        {'url': 'http://foo.com/', 'name': UNICODE_STR}))
    auth_entity.put()

    key = FakeSource.next_key()
    with app.test_request_context(), self.assertRaises(RequestRedirect) as rr:
      state = util.construct_state_param_for_add(feature='publish')
      util.maybe_add_or_delete_source(FakeSource, auth_entity, state)
      self.assertIn(UNICODE_STR, get_flashed_messages()[0])

    self.assertEqual(302, rr.exception.code)
    self.assertEqual(['publish'], key.get().features)

    name = urllib.parse.quote_plus(UNICODE_STR.encode())
    self.assertIn(f'logins="/fake/{key.id()}?{name}";',
                  rr.exception.get_response().headers['Set-Cookie'])

    for feature in None, '':
      key = FakeSource.next_key()
      with app.test_request_context(), self.assertRaises(RequestRedirect) as rr:
        state = util.construct_state_param_for_add(feature)
        util.maybe_add_or_delete_source(FakeSource, auth_entity, state)
      self.assertEqual([], key.get().features)

  def test_maybe_add_or_delete_source_bad_state(self):
    auth_entity = FakeAuthEntity(id='x', user_json='{}')
    auth_entity.put()
    with self.assertRaises(BadRequest):
      util.maybe_add_or_delete_source(FakeSource, auth_entity, 'bad')

  def test_maybe_add_or_delete_source_delete_declined(self):
    state = {
      'feature': 'webmention',
      'operation': 'delete',
    }
    msg = 'If you want to disable, please approve the FakeSource prompt.'

    # no source
    with app.test_request_context():
      with self.assertRaises(RequestRedirect) as rr:
        util.maybe_add_or_delete_source(
          FakeSource, None, util.encode_oauth_state(state))

      self.assert_equals(302, rr.exception.code)
      self.assert_equals('http://localhost/', rr.exception.new_url)
      self.assertEqual([msg], get_flashed_messages())

    # source
    state['source'] = self.sources[0].key.urlsafe().decode()
    with app.test_request_context(), self.assertRaises(RequestRedirect) as rr:
      util.maybe_add_or_delete_source(
        FakeSource, None, util.encode_oauth_state(state))

      self.assert_equals(302, rr.exception.code)
      self.assert_equals(self.source_bridgy_url, rr.exception.new_url)
      self.assertEqual([msg], get_flashed_messages())

  def test_maybe_add_or_delete_without_web_site_redirects_to_edit_websites(self):
    for bad_url in None, 'not>a<url', 'http://fa.ke/xyz':
      auth_entity = FakeAuthEntity(id='x', user_json=json_dumps({'url': bad_url}))
      auth_entity.put()

      key = FakeSource.next_key().urlsafe().decode()
      with app.test_request_context(), self.assertRaises(RequestRedirect) as rr:
        util.maybe_add_or_delete_source(FakeSource, auth_entity, '{}')

      self.assertEqual(302, rr.exception.code)
      self.assert_equals(f'http://localhost/edit-websites?source_key={key}',
                         rr.exception.new_url)

  def test_maybe_add_or_delete_source_username_key_id_disables_other_source(self):
    class UKISource(Source):
      USERNAME_KEY_ID = True
      GR_CLASS = FakeGrSource

      @classmethod
      def new(cls, **kwargs):
        del kwargs['auth_entity']
        return UKISource(username='FoO', domain_urls=['http://foo/'], **kwargs)

    # original entity with capitalized key name
    orig_key = UKISource(id='FoO', features=['listen']).put()

    with app.test_request_context(), self.assertRaises(RequestRedirect) as rr:
      state = util.construct_state_param_for_add(feature='publish')
      util.maybe_add_or_delete_source(UKISource, FakeAuthEntity(id='x'), state)

    self.assertEqual(302, rr.exception.code)
    got = UKISource.get_by_id('foo')
    self.assertEqual('FoO', got.username)
    self.assertEqual(['publish'], got.features)

    # original entity with capitalized key should be disabled
    self.assertEqual([], orig_key.get().features)

  def test_add_to_logins_cookie(self):
    with app.test_request_context(headers={'Cookie': 'logins=/other/1?bob'}):
      listen = util.construct_state_param_for_add(feature='listen')
      auth_entity = FakeAuthEntity(id='x', user_json='{}')
      auth_entity.put()

      id = FakeSource.next_key().id()
      with self.assertRaises(RequestRedirect) as rr:
        util.maybe_add_or_delete_source(FakeSource, auth_entity, listen)

      cookie = 'logins="/fake/{id}?fake|/other/1?bob";'
      self.assertIn(cookie.format(id=id),
                    rr.exception.get_response().headers['Set-Cookie'])

      id = FakeSource.next_key().id()
      with self.assertRaises(RequestRedirect) as rr:
        util.maybe_add_or_delete_source(FakeSource, auth_entity, listen)
      self.assertIn(cookie.format(id=id),
                    rr.exception.get_response().headers['Set-Cookie'])

  def test_get_logins(self):
    for cookie, expected in (
        ('', []),
        ('abc=xyz', []),
        ('logins=', []),
        ('logins=|', []),
        ('logins=/fake/123', [Login('fake', '', '/fake/123')]),
        ('logins=/fake/123?Name', [Login('fake', 'Name', '/fake/123')]),
        ('logins=/fake/123?a%E2%98%95b', [Login('fake', 'a☕b', '/fake/123')]),
        ('logins=/fake/123?Name|/blogger/456?Nombre',
         [Login('fake', 'Name', '/fake/123'),
          Login('blogger', 'Nombre', '/blogger/456'),
         ]),
    ):
      with app.test_request_context(headers={'Cookie': cookie}):
        self.assertCountEqual(expected, util.get_logins())

  def test_logins_cookie_url_decode(self):
    """https://console.cloud.google.com/errors/10588536940780707768?project=brid-gy"""
    with app.test_request_context(headers={'Cookie': 'logins="/fake/123?question%3Fmark"'}):
      self.assertEqual([Login(site='fake', name='question?mark', path='/fake/123')],
                       util.get_logins())

  def test_bad_logins_cookies(self):
    """https://github.com/snarfed/bridgy/issues/601"""
    with app.test_request_context(headers={
        'Cookie': 'OAMAuthnCookie_www.arbeitsagentur.de:443=xyz',
    }):
      self.assertEqual([], util.get_logins())

  def test_prune_activity(self):
    for orig, expected in (
      ({'id': 1, 'content': 'X', 'foo': 'bar'}, {'id': 1, 'content': 'X'}),
      ({'id': 1, 'object': {'objectType': 'note'}}, {'id': 1}),
      ({'id': 1, 'object': {'url': 'http://xyz'}},) * 2,  # no change
      ({'to': [{'objectType': 'group', 'alias': '@public'}]}, {}),
      ({'object': {'to': [{'objectType': 'group', 'alias': '@private'}]}},) * 2,
      ({'id': 1, 'object': {'id': 1}}, {'id': 1}),
      ({'id': 1, 'object': {'id': 2}},) * 2,
      ({'fb_id': 1, 'object': {'fb_object_id': 2}},) * 2,
      ):
      self.assert_equals(expected, util.prune_activity(orig, self.sources[0]))

  def test_get_webmention_target_blocklisted_urls(self):
    for resolve in True, False:
      self.assertTrue(util.get_webmention_target(
        'http://good.com/a', resolve=resolve)[2])
      for bad in ('http://facebook.com/x', 'https://www.facebook.com/y',
                  'http://sub.dom.ain.facebook.com/z'):
        self.assertFalse(util.get_webmention_target(bad, resolve=resolve)[2], bad)

  def test_get_webmention_cleans_redirected_urls(self):
    self.expect_requests_head('http://foo/bar',
                              redirected_url='http://final?utm_source=x')
    self.mox.ReplayAll()

    self.assert_equals(('http://final', 'final', True),
                       util.get_webmention_target('http://foo/bar', resolve=True))
    self.assert_equals(('http://foo/bar', 'foo', True),
                       util.get_webmention_target('http://foo/bar', resolve=False))

  def test_get_webmention_second_redirect_not_text_html(self):
    self.expect_requests_head('http://orig',
                              redirected_url=['http://middle', 'https://end'],
                              content_type='application/pdf')
    self.mox.ReplayAll()
    self.assert_equals(('https://end', 'end', False),
                       util.get_webmention_target('http://orig', resolve=True))

  def test_get_webmention_middle_redirect_blocklisted(self):
    """We should allow blocklisted domains in the middle of a redirect chain.

    ...e.g. Google's redirector https://www.google.com/url?...
    """
    self.expect_requests_head(
      'http://orig',
      redirected_url=['https://www.google.com/url?xyz', 'https://end'])
    self.mox.ReplayAll()
    self.assert_equals(('https://end', 'end', True),
                       util.get_webmention_target('http://orig', resolve=True))

  def test_get_webmention_target_too_big(self):
    self.expect_requests_head('http://orig', response_headers={
      'Content-Length': str(util.MAX_HTTP_RESPONSE_SIZE + 1),
    })
    self.mox.ReplayAll()
    self.assert_equals(('http://orig', 'orig', False),
                       util.get_webmention_target('http://orig'))

  def test_get_webmention_target_not_http_https(self):
    self.assert_equals(('chrome://flags', 'flags', False),
                       util.get_webmention_target('chrome://flags'))

  def test_get_webmention_text_mf2_html(self):
    self.expect_requests_head('http://orig', content_type='text/mf2+html')
    self.mox.ReplayAll()
    self.assert_equals(('http://orig', 'orig', True),
                       util.get_webmention_target('http://orig'))

  def test_requests_get_too_big(self):
    self.expect_requests_get(
      'http://foo/bar', '',
      response_headers={'Content-Length': str(util.MAX_HTTP_RESPONSE_SIZE + 1)})
    self.mox.ReplayAll()

    resp = util.requests_get('http://foo/bar')
    self.assertEqual(util.HTTP_RESPONSE_TOO_BIG_STATUS_CODE, resp.status_code)
    self.assertIn(' larger than our limit ', resp.text)

  def test_requests_get_content_length_not_int(self):
    self.expect_requests_get('http://foo/bar', 'xyz',
                             response_headers={'Content-Length': 'foo'})
    self.mox.ReplayAll()

    resp = util.requests_get('http://foo/bar')
    self.assertEqual(200, resp.status_code)
    self.assertEqual('xyz', resp.text)

  def test_requests_get_url_blocklist(self):
    resp = util.requests_get(next(iter(util.URL_BLOCKLIST)))
    self.assertEqual(util.HTTP_REQUEST_REFUSED_STATUS_CODE, resp.status_code)
    self.assertEqual('Sorry, Bridgy has blocklisted this URL.', resp.text)

  def test_blocklist_localhost_when_deployed(self):
    self.mox.StubOutWithMock(util, 'LOCAL')
    util.LOCAL = False
    for bad in 'http://localhost:8080/', 'http://127.0.0.1/':
      resp = util.requests_get(bad)
      self.assertEqual(util.HTTP_REQUEST_REFUSED_STATUS_CODE, resp.status_code)
      self.assertEqual('Sorry, Bridgy has blocklisted this URL.', resp.text)

  def test_no_accept_header(self):
    self.assertEqual({}, util.request_headers(url='http://foo/bar'))
    self.assertEqual({}, util.request_headers(source=Twitter(id='not-rhiaro')))

    self.expect_requests_get('http://foo/bar', '')
    self.mox.ReplayAll()
    util.requests_get('http://foo/bar')

  def test_rhiaro_accept_header(self):
    """Only send Accept header to rhiaro.co.uk right now.
    https://github.com/snarfed/bridgy/issues/713
    """
    self.assertEqual(util.REQUEST_HEADERS_CONNEG,
                      util.request_headers(url='http://rhiaro.co.uk/'))
    self.assertEqual(util.REQUEST_HEADERS_CONNEG,
                      util.request_headers(source=Twitter(id='rhiaro')))

    self.expect_requests_get('http://rhiaro.co.uk/', '',
                             headers=util.REQUEST_HEADERS_CONNEG)
    self.mox.ReplayAll()
    util.requests_get('http://rhiaro.co.uk/')

  def test_in_webmention_blocklist(self):
    for bad in 't.co', 'x.t.co', 'X.Y.T.CO', 'abc.onion':
      self.assertTrue(util.in_webmention_blocklist(bad), bad)

    for good in 'snarfed.org', 'www.snarfed.org', 't.co.com':
      self.assertFalse(util.in_webmention_blocklist(good), good)

    self.mox.StubOutWithMock(util, 'LOCAL')
    util.LOCAL = False
    self.assertTrue(util.in_webmention_blocklist('localhost'))
    util.LOCAL = True
    self.assertFalse(util.in_webmention_blocklist('localhost'))

  def test_webmention_endpoint_cache_key(self):
    for expected, url in (
        ('W http foo.com', 'http://foo.com/x'),
        ('W https foo.com', 'https://foo.com/x/y'),
        ('W http foo.com /', 'http://foo.com'),
        ('W http foo.com /', 'http://foo.com/'),
    ):
      got = util.webmention_endpoint_cache_key(url)
      self.assertEqual(expected, got, (url, got))

  def test_add_task(self):
    self.expect_task('foo', eta_seconds=123, x='y')
    self.mox.ReplayAll()

    eta = int(util.to_utc_timestamp(util.now_fn())) + 123
    util.add_task('foo', eta_seconds=eta, x='y', z=None)

  def test_host_url(self):
    with app.test_request_context():
      self.assertEqual('http://localhost/', util.host_url())
      self.assertEqual('http://localhost/asdf', util.host_url('asdf'))
      self.assertEqual('http://localhost/foo/bar', util.host_url('/foo/bar'))

    with app.test_request_context(base_url='https://a.xyz', path='/foo'):
      self.assertEqual('https://a.xyz/', util.host_url())
      self.assertEqual('https://a.xyz/asdf', util.host_url('asdf'))
      self.assertEqual('https://a.xyz/foo/bar', util.host_url('/foo/bar'))

  def test_load_source(self):
    key = self.sources[0].key.urlsafe().decode()

    for k in 'SELECT FOO', key, "(SELECT+foo,'qvjkq'))":
      with app.test_request_context(query_string=f'key={k}'), \
           self.assertRaises(BadRequest):
        util.load_source()

    self.sources[0].put()
    with app.test_request_context(query_string=f'key={key}'):
      self.assert_entities_equal(self.sources[0], util.load_source())


class RegistrationCallbackTest(testutil.AppTest):

  class FakeAdd(oauth_views.Callback):
    """Serves the authorization callback when handling a fake source.
    """
    auth_entity = None  # populated in setUp()

    def dispatch_request(self):
      util.maybe_add_or_delete_source(FakeSource, self.auth_entity,
                                      request.values.get('state'))
      return ''

  def setUp(self):
    super().setUp()

    self.app = Flask('RegistrationCallbackTest')
    self.app.config.from_mapping({
      'ENV': 'development',
      'SECRET_KEY': 'sooper seekret',
    })

    self.FakeAdd.auth_entity = FakeAuthEntity(user_json=json_dumps({
      'id': '0123456789',
      'name': 'Fake User',
      'url': 'http://fakeuser.com/',
    }))

    self.start = testutil.FakeStart.as_view('start', '/fake/add')
    self.app.add_url_rule('/fake/start', view_func=self.start, methods=['POST'])
    self.add = self.FakeAdd.as_view('callback', '/fake/callback')
    self.app.add_url_rule('/fake/add', view_func=self.add)

    self.client = self.app.test_client()

  @staticmethod
  def state(**kwargs):
    return urllib.parse.quote_plus(json_dumps({
      'callback': 'http://withknown.com/bridgy_callback',
      'feature': 'listen',
      'operation': 'add',
      **kwargs,
    }, sort_keys=True))

  def assert_auth_url_state(self, response, state):
    self.assertEqual('http://fake/auth/url?' + urllib.parse.urlencode({
      'redirect_uri': f'http://localhost/fake/add?state={state}',
    }), response.headers['Location'])

  def test_basic(self):
    """Run through an authorization back and forth and make sure that
    the external callback makes it all the way through.
    """
    state = self.state()

    self.expect_requests_get(
      'http://fakeuser.com/',
      response='<html><link rel="webmention" href="/webmention"></html>')
    self.mox.ReplayAll()

    resp = self.client.post('/fake/start', data={
        'feature': 'listen',
        'callback': 'http://withknown.com/bridgy_callback',
      })
    self.assert_equals(302, resp.status_code)
    self.assert_auth_url_state(resp, state)

    resp = self.client.get(f'/fake/add?state={state}&oauth_token=fake-token&oauth_token_secret=fake-secret')
    self.assert_equals(302, resp.status_code)
    self.assert_equals(
      'http://withknown.com/bridgy_callback?' + urllib.parse.urlencode([
        ('result', 'success'),
        ('user', 'http://localhost/fake/0123456789'),
        ('key', ndb.Key('FakeSource', '0123456789').urlsafe().decode()),
      ]),
      resp.headers['Location'])

    source = FakeSource.get_by_id('0123456789')
    self.assertTrue(source)
    self.assert_equals('Fake User', source.name)
    self.assert_equals(['listen'], source.features)

  def test_user_url(self):
    """Run through an authorization back and forth with a custom user url
    provided to the auth mechanism
    """
    state = self.state(user_url='https://kylewm.com')

    self.expect_requests_get(
      'https://kylewm.com/',
      response='<html><link rel="webmention" href="/webmention"></html>')
    self.mox.ReplayAll()

    resp = self.client.post('/fake/start', data={
        'feature': 'listen',
        'callback': 'http://withknown.com/bridgy_callback',
        'user_url': 'https://kylewm.com',
      })

    self.assert_equals(302, resp.status_code)
    self.assert_auth_url_state(resp, state)

    resp = self.client.get(f'/fake/add?state={state}&oauth_token=fake-token&oauth_token_secret=fake-secret')
    self.assert_equals(302, resp.status_code)
    self.assert_equals(
      'http://withknown.com/bridgy_callback?' + urllib.parse.urlencode([
        ('result', 'success'),
        ('user', 'http://localhost/fake/0123456789'),
        ('key', ndb.Key('FakeSource', '0123456789').urlsafe().decode()),
      ]),
      resp.headers['Location'])

    source = FakeSource.get_by_id('0123456789')
    self.assertTrue(source)
    self.assert_equals('Fake User', source.name)
    self.assert_equals(['listen'], source.features)
    self.assert_equals(['https://kylewm.com/', 'http://fakeuser.com/'],
                       source.domain_urls)
    self.assert_equals(['kylewm.com', 'fakeuser.com'], source.domains)

  def test_decline(self):
    """Run through an authorization back and forth in the case of a
    decline and make sure that the callback makes it all the way
    through.
    """
    state = self.state(feature='publish')
    self.FakeAdd.auth_entity = None

    resp = self.client.post('/fake/start', data={
      'feature': 'publish',
      'callback': 'http://withknown.com/bridgy_callback',
    })
    self.assert_equals(302, resp.status_code)
    self.assert_auth_url_state(resp, state)

    resp = self.client.get(f'/fake/add?state={state}&denied=1')
    self.assert_equals(302, resp.status_code)
    self.assert_equals('http://withknown.com/bridgy_callback?result=declined',
                       resp.headers['Location'])
