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
    # Test that original post discovery does the reverse lookup
    # to scan author's h-feed for rel=syndication links
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
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="http://author/post/permalink"></a>
      </div>
    </html>"""
    requests.get('http://author',
                 timeout=HTTP_TIMEOUT).AndReturn(resp)

    # syndicated to two places
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

    self.mox.VerifyAll()

  def test_original_post_discovery_no_rework(self):
    # Test that original post discovery fetches and stores all entries
    # up front so that it does not have to reparse the author's h-feed
    # for every new post Test that original post discovery does the
    # reverse lookup to scan author's h-feed for rel=syndication links
    activities = [{
      'id': 'tag:source.com,2014:a',
      'object': {
        'objectType': 'note',
        'id': 'tag:source.com,2014:%d' % idx,
        'url': 'http://source/post/url%d' % idx,
        'content': 'post content without links',
        'to': [{'objectType': 'group', 'alias': '@public'}]
        }
      } for idx in (1, 2, 3)]

    author_feed = """
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="http://author/post/permalink1"></a>
      </div>
      <div class="h-entry">
        <a class="u-url" href="http://author/post/permalink2"></a>
      </div>
      <div class="h-entry">
        <a class="u-url" href="http://author/post/permalink3"></a>
      </div>
    </html>"""

    source = FakeSource()
    source.domain_url = 'http://author'

    self.mox.StubOutWithMock(requests, 'get')

    resp = requests.Response()
    resp.status_code = 200
    resp._content = author_feed
    requests.get('http://author',
                 timeout=HTTP_TIMEOUT).AndReturn(resp)

    # first post is syndicated
    resp = requests.Response()
    resp.status_code = 200
    resp._content = """
    <div class="h-entry">
      <a class="u-url" href="http://author/post/permalink1"></a>
      <a class="u-syndication" href="http://source/post/url1"></a>
    </div>"""

    requests.get('http://author/post/permalink1',
                 timeout=HTTP_TIMEOUT).AndReturn(resp)

    # second post is syndicated
    resp = requests.Response()
    resp.status_code = 200
    resp._content = """
    <div class="h-entry">
      <a class="u-url" href="http://author/post/permalink2"></a>
      <a class="u-syndication" href="http://source/post/url2"></a>
    </div>"""

    requests.get('http://author/post/permalink2',
                 timeout=HTTP_TIMEOUT).AndReturn(resp)

    # third post is not syndicated
    resp = requests.Response()
    resp.status_code = 200
    resp._content = """
    <div class="h-entry">
      <a class="u-url" href="http://author/post/permalink3"></a>
    </div>"""

    requests.get('http://author/post/permalink3',
                 timeout=HTTP_TIMEOUT).AndReturn(resp)

    # the second activity lookup should not make any HTTP requests

    # the third activity lookup will fetch the author's h-feed one more time

    resp = requests.Response()
    resp.status_code = 200
    resp._content = author_feed
    requests.get('http://author', timeout=HTTP_TIMEOUT).AndReturn(resp)

    self.mox.ReplayAll()
    # first activity should trigger all the lookups and storage
    util.original_post_discovery(source, activities[0])

    self.assertEquals('http://author/post/permalink1',
                      activities[0]['object']['tags'][0]['url'])

    # make sure things are where we want them
    r = SyndicatedPost.query_by_original('http://author/post/permalink1')
    self.assertEquals('http://source/post/url1', r.syndication)
    r = SyndicatedPost.query_by_syndication('http://source/post/url1')
    self.assertEquals('http://author/post/permalink1', r.original)

    r = SyndicatedPost.query_by_original('http://author/post/permalink2')
    self.assertEquals('http://source/post/url2', r.syndication)
    r = SyndicatedPost.query_by_syndication('http://source/post/url2')
    self.assertEquals('http://author/post/permalink2', r.original)

    r = SyndicatedPost.query_by_original('http://author/post/permalink3')
    self.assertEquals(None, r.syndication)

    # second lookup should require no additional HTTP requests.
    # the second syndicated post should be linked up to the second permalink.
    util.original_post_discovery(source, activities[1])
    self.assertEquals('http://author/post/permalink2',
                      activities[1]['object']['tags'][0]['url'])

    # third activity lookup.
    # since we didn't find a back-link for the third syndicated post,
    # it should fetch the author's feed again, but seeing no new
    # posts, it should not follow any of the permalinks

    util.original_post_discovery(source, activities[2])
    self.assertFalse(activities[2]['object'].get('tags'))

    # should have saved a blank to prevent subsequent checks of this
    # syndicated post from fetching the h-feed again
    r = SyndicatedPost.query_by_syndication('http://source/post/url3')
    self.assertEquals(None, r.original)

    # confirm that we do not fetch the h-feed again for the same
    # syndicated post
    util.original_post_discovery(source, activities[2])
    self.assertFalse(activities[2]['object'].get('tags'))

    self.mox.VerifyAll()
