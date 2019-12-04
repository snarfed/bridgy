# coding=utf-8
"""Unit tests for util.py."""
from __future__ import unicode_literals
from __future__ import absolute_import

from future.utils import native_str
from future import standard_library
standard_library.install_aliases()
from builtins import next, str
import datetime
import time
import urllib.request, urllib.parse, urllib.error

from appengine_config import HTTP_TIMEOUT

from google.cloud import ndb
from oauth_dropins.webutil.testutil import requests_response
from oauth_dropins.webutil.util import json_dumps, json_loads
import requests
import webapp2
from webmentiontools import send
from webob import exc

from . import testutil
from .testutil import FakeAuthEntity, FakeSource
from twitter import Twitter
import util
from util import Login

# the character in the middle is an unusual unicode character
UNICODE_STR = 'a ✁ b'


class UtilTest(testutil.ModelsTest):

  def setUp(self):
    super(UtilTest, self).setUp()
    util.now_fn = lambda: datetime.datetime(2000, 1, 1)

  def test_maybe_add_or_delete_source(self):
    auth_entity = FakeAuthEntity(id='x', user_json=json_dumps(
        {'url': 'http://foo.com/', 'name': UNICODE_STR}))
    auth_entity.put()
    src = self.handler.maybe_add_or_delete_source(
      FakeSource, auth_entity,
      self.handler.construct_state_param_for_add(feature='publish'))
    self.assertEquals(['publish'], src.features)

    self.assertEquals(302, self.response.status_int)
    parsed = urllib.parse.urlparse(self.response.headers['Location'])
    self.assertIn(UNICODE_STR, urllib.parse.unquote_plus(parsed.fragment))
    self.assertEquals(
      'logins="/fake/%s?%s"; expires=2001-12-31 00:00:00; Path=/' %
        (src.key.id(), urllib.parse.quote_plus(UNICODE_STR.encode('utf-8'))),
      self.response.headers['Set-Cookie'])

    for feature in None, '':
      src = self.handler.maybe_add_or_delete_source(
        FakeSource, auth_entity,
        self.handler.construct_state_param_for_add(feature))
      self.assertEquals([], src.features)

  def test_maybe_add_or_delete_source_bad_state(self):
    auth_entity = FakeAuthEntity(id='x', user_json='{}')
    auth_entity.put()
    with self.assertRaises(exc.HTTPBadRequest):
      self.handler.maybe_add_or_delete_source(FakeSource, auth_entity, 'bad')

  def test_maybe_add_or_delete_source_delete_declined(self):
    state = {
      'feature': 'webmention',
      'operation': 'delete',
    }
    msg = '#!If%20you%20want%20to%20disable%2C%20please%20approve%20the%20FakeSource%20prompt.'

    # no source
    self.assertIsNone(self.handler.maybe_add_or_delete_source(
      FakeSource, None, util.encode_oauth_state(state)))
    self.assert_equals(302, self.handler.response.status_code)
    self.assert_equals('http://localhost/' + msg,
                       self.handler.response.headers['location'])

    # source
    state['source'] = self.sources[0].key.urlsafe()
    self.assertIsNone(self.handler.maybe_add_or_delete_source(
      FakeSource, None, util.encode_oauth_state(state)))
    self.assert_equals(302, self.handler.response.status_code)
    self.assert_equals(self.sources[0].bridgy_url(self.handler) + msg,
                       self.handler.response.headers['location'])

  def test_maybe_add_or_delete_without_web_site_redirects_to_edit_websites(self):
    for bad_url in None, 'not>a<url', 'http://fa.ke/xyz':
      auth_entity = FakeAuthEntity(id='x', user_json=json_dumps({'url': bad_url}))
      auth_entity.put()
      source = self.handler.maybe_add_or_delete_source(FakeSource, auth_entity, '{}')

      self.assertEquals(302, self.handler.response.status_code)
      self.assert_equals(
        'http://localhost/edit-websites?source_key=%s' % source.key.urlsafe(),
        self.handler.response.headers['Location'])

  def test_add_to_logins_cookie(self):
    listen = self.handler.construct_state_param_for_add(feature='listen')
    auth_entity = FakeAuthEntity(id='x', user_json='{}')
    auth_entity.put()

    self.request.headers['Cookie'] = 'logins=/other/1?bob'
    src1 = self.handler.maybe_add_or_delete_source(FakeSource, auth_entity, listen)
    cookie = 'logins="/fake/%s?fake|/other/1?bob"; expires=2001-12-31 00:00:00; Path=/'
    self.assertEquals(cookie % src1.key.id(), self.response.headers['Set-Cookie'])

    src2 = self.handler.maybe_add_or_delete_source(FakeSource, auth_entity, listen)
    self.request.headers['Cookie'] = \
      'logins="/fake/%s?fake|/other/1?bob"' % src2.key.id()
    self.assertEquals(cookie % src2.key.id(), self.response.headers['Set-Cookie'])

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
      self.request.headers['Cookie'] = cookie
      self.assertItemsEqual(expected, self.handler.get_logins())

  def test_logins_cookie_url_decode(self):
    """https://console.cloud.google.com/errors/10588536940780707768?project=brid-gy"""
    self.request.headers['Cookie'] = 'logins="/fake/123?question%3Fmark"'
    self.assertEquals([Login(site='fake', name='question?mark', path='/fake/123')],
                      self.handler.get_logins())

  def test_bad_logins_cookies(self):
    """https://github.com/snarfed/bridgy/issues/601"""
    self.request.headers['Cookie'] = 'OAMAuthnCookie_www.arbeitsagentur.de:443=xyz'
    self.assertEquals([], self.handler.get_logins())

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

  def test_webmention_tools_relative_webmention_endpoint_in_body(self):
    requests.get('http://target/', verify=False).AndReturn(requests_response("""
<html><meta>
<link rel="webmention" href="/endpoint">
</meta></html>"""))
    self.mox.ReplayAll()

    mention = send.WebmentionSend('http://source/', 'http://target/')
    mention.requests_kwargs = {}
    mention._discoverEndpoint()
    self.assertEquals('http://target/endpoint', mention.receiver_endpoint)

  def test_webmention_tools_relative_webmention_endpoint_in_header(self):
    requests.get('http://target/', verify=False).AndReturn(requests_response(
      '', headers={'Link': '</endpoint>; rel="webmention"'}))
    self.mox.ReplayAll()

    mention = send.WebmentionSend('http://source/', 'http://target/')
    mention.requests_kwargs = {}
    mention._discoverEndpoint()
    self.assertEquals('http://target/endpoint', mention.receiver_endpoint)

  def test_get_webmention_target_blacklisted_urls(self):
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

  def test_get_webmention_middle_redirect_blacklisted(self):
    """We should allow blacklisted domains in the middle of a redirect chain.

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

  def test_registration_callback(self):
    """Run through an authorization back and forth and make sure that
    the external callback makes it all the way through.
    """
    encoded_state = urllib.parse.quote_plus(json_dumps({
      'callback': 'http://withknown.com/bridgy_callback',
      'feature': 'listen',
      'operation': 'add',
    }, sort_keys=True))

    application = webapp2.WSGIApplication([
      ('/fakesource/start', testutil.FakeStartHandler),
      ('/fakesource/add', testutil.FakeAddHandler),
    ])

    self.expect_webmention_requests_get(
      'http://fakeuser.com/',
      response='<html><link rel="webmention" href="/webmention"></html>')

    self.mox.ReplayAll()

    resp = application.get_response(
      '/fakesource/start', method='POST', body=native_str(urllib.parse.urlencode({
        'feature': 'listen',
        'callback': 'http://withknown.com/bridgy_callback',
      })))

    expected_auth_url = 'http://fake/auth/url?' + urllib.parse.urlencode({
      'redirect_uri': 'http://localhost/fakesource/add?state='
      + encoded_state,
    })

    self.assert_equals(302, resp.status_code)
    self.assert_equals(expected_auth_url, resp.headers['location'])

    resp = application.get_response(native_str(
      '/fakesource/add?state=' + encoded_state +
      '&oauth_token=fake-token&oauth_token_secret=fake-secret'))

    self.assert_equals(302, resp.status_code)
    self.assert_equals(
      'http://withknown.com/bridgy_callback?' + urllib.parse.urlencode([
        ('result', 'success'),
        ('key', ndb.Key('FakeSource', '0123456789').urlsafe()),
        ('user', 'http://localhost/fake/0123456789')]),
      resp.headers['location'])
    self.assertEquals(
      'logins="/fake/0123456789?Fake+User"; expires=2001-12-31 00:00:00; Path=/',
      resp.headers['Set-Cookie'])

    source = FakeSource.get_by_id('0123456789')
    self.assertTrue(source)
    self.assert_equals('Fake User', source.name)
    self.assert_equals(['listen'], source.features)

  def test_registration_with_user_url(self):
    """Run through an authorization back and forth with a custom user url
    provided to the auth mechanism
    """
    encoded_state = urllib.parse.quote_plus(json_dumps({
      'callback': 'http://withknown.com/bridgy_callback',
      'feature': 'listen',
      'operation': 'add',
      'user_url': 'https://kylewm.com',
    }, sort_keys=True))

    application = webapp2.WSGIApplication([
      ('/fakesource/start', testutil.FakeStartHandler),
      ('/fakesource/add', testutil.FakeAddHandler),
    ])

    self.expect_webmention_requests_get(
      'https://kylewm.com/',
      response='<html><link rel="webmention" href="/webmention"></html>')

    self.mox.ReplayAll()

    resp = application.get_response(
      '/fakesource/start', method='POST', body=native_str(urllib.parse.urlencode({
        'feature': 'listen',
        'callback': 'http://withknown.com/bridgy_callback',
        'user_url': 'https://kylewm.com',
      })))

    expected_auth_url = 'http://fake/auth/url?' + urllib.parse.urlencode({
      'redirect_uri': 'http://localhost/fakesource/add?state='
      + encoded_state,
    })

    self.assert_equals(302, resp.status_code)
    self.assert_equals(expected_auth_url, resp.headers['location'])

    resp = application.get_response(native_str(
      '/fakesource/add?state=' + encoded_state +
      '&oauth_token=fake-token&oauth_token_secret=fake-secret'))

    self.assert_equals(302, resp.status_code)
    self.assert_equals(
      'http://withknown.com/bridgy_callback?' + urllib.parse.urlencode([
        ('result', 'success'),
        ('key', ndb.Key('FakeSource', '0123456789').urlsafe()),
        ('user', 'http://localhost/fake/0123456789')]),
      resp.headers['location'])
    self.assertEquals(
      'logins="/fake/0123456789?Fake+User"; expires=2001-12-31 00:00:00; Path=/',
      resp.headers['Set-Cookie'])

    source = FakeSource.get_by_id('0123456789')
    self.assertTrue(source)
    self.assert_equals('Fake User', source.name)
    self.assert_equals(['listen'], source.features)
    self.assert_equals(['https://kylewm.com/', 'http://fakeuser.com/'],
                       source.domain_urls)
    self.assert_equals(['kylewm.com', 'fakeuser.com'], source.domains)

  def test_registration_decline(self):
    """Run through an authorization back and forth in the case of a
    decline and make sure that the callback makes it all the way
    through.
    """
    encoded_state = urllib.parse.quote_plus(json_dumps({
      'callback': 'http://withknown.com/bridgy_callback',
      'feature': 'publish',
      'operation': 'add',
    }, sort_keys=True))

    application = webapp2.WSGIApplication([
      ('/fakesource/start', testutil.FakeStartHandler),
      ('/fakesource/add', testutil.FakeAddHandler.with_auth(None)),
    ])

    resp = application.get_response(
      '/fakesource/start', method='POST', body=native_str(urllib.parse.urlencode({
        'feature': 'publish',
        'callback': 'http://withknown.com/bridgy_callback',
      })))

    expected_auth_url = 'http://fake/auth/url?' + urllib.parse.urlencode({
      'redirect_uri': 'http://localhost/fakesource/add?state='
      + encoded_state,
    })

    self.assert_equals(302, resp.status_code)
    self.assert_equals(expected_auth_url, resp.headers['location'])
    self.assertNotIn('Set-Cookie', resp.headers)

    resp = application.get_response(
      '/fakesource/add?state=%s&denied=1' % encoded_state)
    self.assert_equals(302, resp.status_code)
    self.assert_equals('http://withknown.com/bridgy_callback?result=declined',
                       resp.headers['location'])

  def test_requests_get_too_big(self):
    self.expect_requests_get(
      'http://foo/bar', '',
      response_headers={'Content-Length': str(util.MAX_HTTP_RESPONSE_SIZE + 1)})
    self.mox.ReplayAll()

    resp = util.requests_get('http://foo/bar')
    self.assertEquals(util.HTTP_RESPONSE_TOO_BIG_STATUS_CODE, resp.status_code)
    self.assertIn(' larger than our limit ', resp.content)

  def test_requests_get_content_length_not_int(self):
    self.expect_requests_get('http://foo/bar', 'xyz',
                             response_headers={'Content-Length': 'foo'})
    self.mox.ReplayAll()

    resp = util.requests_get('http://foo/bar')
    self.assertEquals(200, resp.status_code)
    self.assertEquals('xyz', resp.content)

  def test_requests_get_url_blacklist(self):
    resp = util.requests_get(next(iter(util.URL_BLACKLIST)))
    self.assertEquals(util.HTTP_REQUEST_REFUSED_STATUS_CODE, resp.status_code)
    self.assertEquals('Sorry, Bridgy has blacklisted this URL.', resp.content)

  def test_no_accept_header(self):
    self.assertEquals(util.REQUEST_HEADERS,
                      util.request_headers(url='http://foo/bar'))
    self.assertEquals(util.REQUEST_HEADERS,
                      util.request_headers(source=Twitter(id='not-rhiaro')))

    self.expect_requests_get('http://foo/bar', '', headers=util.REQUEST_HEADERS)
    self.mox.ReplayAll()
    util.requests_get('http://foo/bar')

  def test_rhiaro_accept_header(self):
    """Only send Accept header to rhiaro.co.uk right now.
    https://github.com/snarfed/bridgy/issues/713
    """
    self.assertEquals(util.REQUEST_HEADERS_CONNEG,
                      util.request_headers(url='http://rhiaro.co.uk/'))
    self.assertEquals(util.REQUEST_HEADERS_CONNEG,
                      util.request_headers(source=Twitter(id='rhiaro')))

    self.expect_requests_get('http://rhiaro.co.uk/', '',
                             headers=util.REQUEST_HEADERS_CONNEG)
    self.mox.ReplayAll()
    util.requests_get('http://rhiaro.co.uk/')

  def test_in_webmention_blacklist(self):
    for bad in 't.co', 'x.t.co', 'x.y.t.co', 'abc.onion':
      self.assertTrue(util.in_webmention_blacklist(bad), bad)

    for good in 'snarfed.org', 'www.snarfed.org', 't.co.com':
      self.assertFalse(util.in_webmention_blacklist(good), good)

  def test_webmention_endpoint_cache_key(self):
    for expected, url in (
        ('W http foo.com', 'http://foo.com/x'),
        ('W https foo.com', 'https://foo.com/x/y'),
        ('W http foo.com /', 'http://foo.com'),
        ('W http foo.com /', 'http://foo.com/'),
    ):
      got = util.webmention_endpoint_cache_key(url)
      self.assertEquals(expected, got, (url, got))
