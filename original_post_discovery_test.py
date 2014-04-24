# coding=utf-8
"""Unit tests for original_post_discovery.py
"""

from appengine_config import HTTP_TIMEOUT
from models import SyndicatedPost
from testutil import FakeSource
import requests
import logging
import testutil
import json
from original_post_discovery import original_post_discovery
import tasks
import handlers
from activitystreams import microformats2

class OriginalPostDiscoveryTest(testutil.ModelsTest):

  def test_single_post(self):
    # Test that original post discovery does the reverse lookup
    # to scan author's h-feed for rel=syndication links
    activity = self.activities[0]
    activity['object']['content'] = 'post content without backlink'
    activity['object']['url'] = 'http://fa.ke/post/url'

    # silo domain is fa.ke
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
    <link rel="syndication" href="http://not.real/statuses/postid">
    <link rel="syndication" href="http://fa.ke/post/url">
    <div class="h-entry">
      <a class="u-url" href="http://author/post/permalink"></a>
      <a class="u-syndication" href="http://fa.ke/post/url"></a>
      <a class="u-syndication" href="http://not.real/statuses/postid"></a>
    </div>"""

    requests.get('http://author/post/permalink',
                 timeout=HTTP_TIMEOUT).AndReturn(resp)

    self.mox.ReplayAll()
    logging.debug("Original post discovery %s -> %s", source, activity)
    original_post_discovery(source, activity)

    # tags = 2 original + 1 discovered
    self.assertEquals(3, len(activity['object']['tags']))
    self.assertEquals({'url': 'http://author/post/permalink',
                       'objectType': 'article'},
                      activity['object']['tags'][2])

    origurls = [r.original for r in SyndicatedPost.query(ancestor=source.key)]
    self.assertEquals([u'http://author/post/permalink'], origurls)

    # for now only syndicated posts belonging to this source are stored
    syndurls = list(r.syndication for r
                    in SyndicatedPost.query(ancestor=source.key))

    self.assertEquals([u'http://fa.ke/post/url'], syndurls)

  def test_additional_requests_do_not_require_rework(self):
    # Test that original post discovery fetches and stores all entries
    # up front so that it does not have to reparse the author's h-feed
    # for every new post Test that original post discovery does the
    # reverse lookup to scan author's h-feed for rel=syndication links

    for idx, activity in enumerate(self.activities):
        activity['object']['content'] = 'post content without backlinks'
        activity['object']['url'] = 'http://fa.ke/post/url%d' % (idx + 1)

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
      <a class="u-syndication" href="http://fa.ke/post/url1"></a>
    </div>"""

    requests.get('http://author/post/permalink1',
                 timeout=HTTP_TIMEOUT).InAnyOrder().AndReturn(resp)

    # second post is syndicated
    resp = requests.Response()
    resp.status_code = 200
    resp._content = """
    <div class="h-entry">
      <a class="u-url" href="http://author/post/permalink2"></a>
      <a class="u-syndication" href="http://fa.ke/post/url2"></a>
    </div>"""

    requests.get('http://author/post/permalink2',
                 timeout=HTTP_TIMEOUT).InAnyOrder().AndReturn(resp)

    # third post is not syndicated
    resp = requests.Response()
    resp.status_code = 200
    resp._content = """
    <div class="h-entry">
      <a class="u-url" href="http://author/post/permalink3"></a>
    </div>"""

    requests.get('http://author/post/permalink3',
                 timeout=HTTP_TIMEOUT).InAnyOrder().AndReturn(resp)

    # the second activity lookup should not make any HTTP requests

    # the third activity lookup will fetch the author's h-feed one more time

    resp = requests.Response()
    resp.status_code = 200
    resp._content = author_feed
    requests.get('http://author', timeout=HTTP_TIMEOUT).AndReturn(resp)

    self.mox.ReplayAll()
    # first activity should trigger all the lookups and storage
    original_post_discovery(source, self.activities[0])

    self.assertEquals('http://author/post/permalink1',
                      self.activities[0]['object']['tags'][2]['url'])

    # make sure things are where we want them
    r = SyndicatedPost.query_by_original(source, 'http://author/post/permalink1')
    self.assertEquals('http://fa.ke/post/url1', r.syndication)
    r = SyndicatedPost.query_by_syndication(source, 'http://fa.ke/post/url1')
    self.assertEquals('http://author/post/permalink1', r.original)

    r = SyndicatedPost.query_by_original(source, 'http://author/post/permalink2')
    self.assertEquals('http://fa.ke/post/url2', r.syndication)
    r = SyndicatedPost.query_by_syndication(source, 'http://fa.ke/post/url2')
    self.assertEquals('http://author/post/permalink2', r.original)

    r = SyndicatedPost.query_by_original(source, 'http://author/post/permalink3')
    self.assertEquals(None, r.syndication)

    # second lookup should require no additional HTTP requests.
    # the second syndicated post should be linked up to the second permalink.
    original_post_discovery(source, self.activities[1])
    self.assertEquals('http://author/post/permalink2',
                      self.activities[1]['object']['tags'][2]['url'])

    # third activity lookup.
    # since we didn't find a back-link for the third syndicated post,
    # it should fetch the author's feed again, but seeing no new
    # posts, it should not follow any of the permalinks

    original_post_discovery(source, self.activities[2])
    # should have found no new tags
    self.assertEquals(2, len(self.activities[2]['object'].get('tags')))

    # should have saved a blank to prevent subsequent checks of this
    # syndicated post from fetching the h-feed again
    r = SyndicatedPost.query_by_syndication(source, 'http://fa.ke/post/url3')
    self.assertEquals(None, r.original)

    # confirm that we do not fetch the h-feed again for the same
    # syndicated post
    original_post_discovery(source, self.activities[2])
    # should be no new tags
    self.assertEquals(2, len(self.activities[2]['object'].get('tags')))

  def test_no_duplicate_links(self):
    source = self.sources[0]
    source.domain_url = 'http://target1'

    activity = self.activities[0]
    activity['object']['content'] = "with a backlink http://target1/post/url"
    activity['object']['url'] = "http://fa.ke/post/url"

    original = 'http://target1/post/url'
    syndicated = 'http://fa.ke/post/url'

    self.mox.StubOutWithMock(requests, 'get')

    resp = requests.Response()
    resp.status_code = 200
    resp._content = """
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="%s"></a>
      </div>
    </html>""" % original
    requests.get('http://target1',
                 timeout=HTTP_TIMEOUT).AndReturn(resp)

    # syndicated to two places
    resp = requests.Response()
    resp.status_code = 200
    resp._content = """
    <div class="h-entry">
      <a class="u-url" href="%s"></a>
      <a class="u-syndication" href="%s"></a>
    </div>""" % (original, syndicated)

    requests.get(original,
                 timeout=HTTP_TIMEOUT).AndReturn(resp)

    self.mox.ReplayAll()
    logging.debug("Original post discovery %s -> %s", source, activity)

    wmtargets = tasks.get_webmention_targets(source, activity)
    # activity *will* have a duplicate tag for the original post, one
    # discovered in the post content, one from the rel=syndication
    # lookup.
    self.assertEquals([None, None, original, original],
                      [tag.get('url') for tag in activity['object']['tags']])
    # webmention targets converts to a set to remove duplicates
    self.assertEquals(set([original]), wmtargets)

    # TODO ensure that handlers.add_original_post_urls doesn't create duplicate links
    #handler = handlers.ItemHandler()
    #handler.source = source
    #handler.add_original_post_urls(0, activity, 'inReplyTo')
    #activity_json = microformats2.object_to_json(activity)
