# coding=utf-8
"""Unit tests for original_post_discovery.py
"""

from appengine_config import HTTP_TIMEOUT
from models import SyndicatedPost
from testutil import FakeSource
import requests
import logging
import testutil
from original_post_discovery import original_post_discovery


class OriginalPostDiscoveryTest(testutil.ModelsTest):

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
        'to': [{'objectType': 'group', 'alias': '@public'}],
        },
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
    logging.debug("Original post discovery %s -> %s", source, activity)
    original_post_discovery(source, activity)
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
        'to': [{'objectType': 'group', 'alias': '@public'}],
        },
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
    original_post_discovery(source, activities[0])

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
    original_post_discovery(source, activities[1])
    self.assertEquals('http://author/post/permalink2',
                      activities[1]['object']['tags'][0]['url'])

    # third activity lookup.
    # since we didn't find a back-link for the third syndicated post,
    # it should fetch the author's feed again, but seeing no new
    # posts, it should not follow any of the permalinks

    original_post_discovery(source, activities[2])
    self.assertFalse(activities[2]['object'].get('tags'))

    # should have saved a blank to prevent subsequent checks of this
    # syndicated post from fetching the h-feed again
    r = SyndicatedPost.query_by_syndication('http://source/post/url3')
    self.assertEquals(None, r.original)

    # confirm that we do not fetch the h-feed again for the same
    # syndicated post
    original_post_discovery(source, activities[2])
    self.assertFalse(activities[2]['object'].get('tags'))

    self.mox.VerifyAll()
