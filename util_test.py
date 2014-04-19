# coding=utf-8
"""Unit tests for util.py.
"""

import json
import urllib
import urlparse

import requests

from appengine_config import HTTP_TIMEOUT
import testutil
from testutil import FakeAuthEntity, FakeSource
import util
from models import SyndicatedPost

# the invisible character in the middle is an unusual unicode character
UNICODE_STR = u'a ✁ b'


class UtilTest(testutil.ModelsTest):

  def setUp(self):
    super(testutil.ModelsTest, self).setUp()
    util.WEBMENTION_BLACKLIST.add('fa.ke')

  def test_follow_redirects(self):
    self.mox.StubOutWithMock(requests, 'head', use_mock_anything=True)
    resp = requests.Response()
    resp.url = 'http://final/url'
    resp.headers['content-type'] = 'text/html'
    requests.head('http://will/redirect', allow_redirects=True, timeout=HTTP_TIMEOUT
                  ).AndReturn(resp)

    self.mox.ReplayAll()
    self.assert_equals('http://final/url',
                       util.follow_redirects('http://will/redirect').url)

    # the result should now be in memcache, so we shouldn't fetch the URL again
    self.assert_equals('http://final/url',
                       util.follow_redirects('http://will/redirect').url)


  def test_follow_redirects_with_refresh_header(self):
    self.mox.StubOutWithMock(requests, 'head', use_mock_anything=True)
    resp = requests.Response()
    resp.headers['refresh'] = '0; url=http://refresh'
    requests.head('http://will/redirect', allow_redirects=True, timeout=HTTP_TIMEOUT
                  ).AndReturn(resp)

    resp = requests.Response()
    resp.url = 'http://final'
    requests.head('http://refresh', allow_redirects=True, timeout=HTTP_TIMEOUT
                  ).AndReturn(resp)

    self.mox.ReplayAll()
    self.assert_equals('http://final',
                       util.follow_redirects('http://will/redirect').url)

  def test_follow_redirects_defaults_scheme_to_http(self):
    self.mox.StubOutWithMock(requests, 'head', use_mock_anything=True)
    resp = requests.Response()
    resp.url = 'http://final'
    requests.head('http://foo/bar', allow_redirects=True, timeout=HTTP_TIMEOUT
                  ).AndReturn(resp)

    self.mox.ReplayAll()
    self.assert_equals('http://final', util.follow_redirects('foo/bar').url)

  def test_maybe_add_or_delete_source(self):
    # profile url with valid domain is required for publish
    for bad_url in None, 'not>a<url', 'http://fa.ke/xyz':
      auth_entity = FakeAuthEntity(id='x', user_json=json.dumps({'url': bad_url}))
      auth_entity.put()
      self.assertIsNone(self.handler.maybe_add_or_delete_source(
          FakeSource, auth_entity, 'publish'))

    auth_entity = FakeAuthEntity(id='x', user_json=json.dumps(
        {'url': 'http://foo.com/', 'name': UNICODE_STR}))
    auth_entity.put()
    src = self.handler.maybe_add_or_delete_source(FakeSource, auth_entity, 'publish')
    self.assertEquals(['publish'], src.features)

    self.assertEquals(302, self.handler.response.status_int)
    parsed = urlparse.urlparse(self.handler.response.headers['Location'])
    self.assertIn(UNICODE_STR, urllib.unquote_plus(parsed.fragment).decode('utf-8'))

    for feature in None, '':
      src = self.handler.maybe_add_or_delete_source(FakeSource, auth_entity, feature)
      self.assertEquals([], src.features)

  def test_prune_activity(self):
    for orig, expected in (
      ({'id': 1, 'content': 'X', 'foo': 'bar'}, {'id': 1, 'content': 'X'}),
      ({'id': 1, 'object': {'objectType': 'note'}}, {'id': 1}),
        ({'id': 1, 'object': {'url': 'http://xyz'}},) * 2,  # no change
      ({'to': [{'objectType': 'group', 'alias': '@public'}]}, {}),
      ({'object': {'to': [{'objectType': 'group', 'alias': '@private'}]}},) * 2,
      ({'id': 1, 'object': {'id': 1}}, {'id': 1}),
      ({'id': 1, 'object': {'id': 2}},) * 2,
      ):
      self.assert_equals(expected, util.prune_activity(orig))

  def test_original_post_discovery(self):
    activity = {
      'id': 'tag:source.com,2014:a',
      'object': {
        'objectType': 'note',
        'id': 'tag:source.com,2014:a',
        'url': 'http://source/post/url',
        'content': 'post content without links',
        'to': [{'objectType': 'group', 'alias': '@public'}]
        }
      }

    source = FakeSource()
    source.domain_url = 'http://author'

    self.mox.StubOutWithMock(requests, 'get')

    resp = requests.Response()
    resp.status_code = 200
    resp._content = """
    <html class="u-feed">
      <div class="h-entry">
        <a class="u-url" href="http://author/post/permalink"></a>
      </div>
    </html>"""
    requests.get('http://author',
                 timeout=HTTP_TIMEOUT).AndReturn(resp)

    resp = requests.Response()
    resp.status_code = 200
    resp._content = """
    <link rel="syndication" href="http://anotherSource/statuses/postid">
    <link rel="syndication" href="http://source/post/url">
    <div class="h-entry">
      <a class="u-url" href="http://author/post/permalink"></a>
      <a class="u-syndication" href="http://source/post/url"></a>
      <a class="u-syndication" href="http://anotherSource/statuses/postid"></a>
    </div>"""

    requests.get('http://author/post/permalink',
                 timeout=HTTP_TIMEOUT).AndReturn(resp)

    self.mox.ReplayAll()
    util.original_post_discovery(source, activity)
    self.assertEquals(1, len(activity['object']['tags']))
    self.assertEquals({'url': 'http://author/post/permalink',
                       'objectType': 'article'},
                      activity['object']['tags'][0])

    origurls = list(r.original for r in SyndicatedPost.query().filter(
      SyndicatedPost.syndication == 'http://source/post/url'))
    self.assertEquals([u'http://author/post/permalink'], origurls)

    syndurls = list(r.syndication for r in SyndicatedPost.query().filter(
      SyndicatedPost.original == 'http://author/post/permalink'))
    self.assertEquals(2, len(syndurls))
    self.assertEquals(set([u'http://source/post/url',
                           u'http://anotherSource/statuses/postid']),
                      set(syndurls))
