# coding=utf-8
"""Unit tests for original_post_discovery.py
"""
import logging
import original_post_discovery
import requests
import tasks
import testutil

from appengine_config import HTTP_TIMEOUT
from models import SyndicatedPost
from testutil import FakeSource


class OriginalPostDiscoveryTest(testutil.ModelsTest):

  def test_single_post(self):
    """Test that original post discovery does the reverse lookup to scan
    author's h-feed for rel=syndication links

    """
    activity = self.activities[0]
    activity['object']['content'] = 'post content without backlink'
    activity['object']['url'] = 'http://fa.ke/post/url'

    # silo domain is fa.ke
    source = self.sources[0]
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
    original_post_discovery.discover(source, activity)

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
    """Test that original post discovery fetches and stores all entries up
    front so that it does not have to reparse the author's h-feed for
    every new post Test that original post discovery does the reverse
    lookup to scan author's h-feed for rel=syndication links

    """

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

    source = self.sources[0]
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
    original_post_discovery.discover(source, self.activities[0])

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
    original_post_discovery.discover(source, self.activities[1])
    self.assertEquals('http://author/post/permalink2',
                      self.activities[1]['object']['tags'][2]['url'])

    # third activity lookup.
    # since we didn't find a back-link for the third syndicated post,
    # it should fetch the author's feed again, but seeing no new
    # posts, it should not follow any of the permalinks

    original_post_discovery.discover(source, self.activities[2])
    # should have found no new tags
    self.assertEquals(2, len(self.activities[2]['object'].get('tags')))

    # should have saved a blank to prevent subsequent checks of this
    # syndicated post from fetching the h-feed again
    r = SyndicatedPost.query_by_syndication(source, 'http://fa.ke/post/url3')
    self.assertEquals(None, r.original)

    # confirm that we do not fetch the h-feed again for the same
    # syndicated post
    original_post_discovery.discover(source, self.activities[2])
    # should be no new tags
    self.assertEquals(2, len(self.activities[2]['object'].get('tags')))

  def test_no_duplicate_links(self):
    """Make sure that a link found by both original-post-discovery and
    posse-post-discovery will not result in two webmentions being sent

    """
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
    requests.get('http://target1', timeout=HTTP_TIMEOUT)\
            .AndReturn(resp)

    resp = requests.Response()
    resp.status_code = 200
    resp._content = """
    <div class="h-entry">
      <a class="u-url" href="%s"></a>
      <a class="u-syndication" href="%s"></a>
    </div>""" % (original, syndicated)

    requests.get(original, timeout=HTTP_TIMEOUT)\
            .AndReturn(resp)

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

    # TODO ensure that handlers.add_original_post_urls doesn't create
    #  duplicate links
    # handler = handlers.ItemHandler()
    # handler.source = source
    # handler.add_original_post_urls(0, activity, 'inReplyTo')
    # activity_json = microformats2.object_to_json(activity)

  def test_rel_feed_link(self):
    """Check that we follow the rel=feed link when looking for the
    author's full feed URL

    """
    source = self.sources[0]
    source.domain_url = 'http://author'
    activity = self.activities[0]

    self.mox.StubOutWithMock(requests, 'get')

    resp = requests.Response()
    resp.status_code = 200
    resp._content = """
    <html>
      <head>
        <link rel="feed" type="text/html" href="try_this.html">
        <link rel="alternate" type="application/xml" href="not_this.html">
        <link rel="alternate" type="application/xml" href="nor_this.html">
      </head>
    </html>"""
    requests.get('http://author', timeout=HTTP_TIMEOUT)\
            .AndReturn(resp)

    resp = requests.Response()
    resp.status_code = 200
    resp._content = """
    <html class="h-feed">
      <body>
        <div class="h-entry">Hi</div>
      </body>
    </html>"""
    requests.get('http://author/try_this.html', timeout=HTTP_TIMEOUT)\
            .AndReturn(resp)

    self.mox.ReplayAll()
    logging.debug("Original post discovery %s -> %s", source, activity)
    original_post_discovery.discover(source, activity)

  def test_no_h_entries(self):
    """Make sure nothing bad happens when fetching a feed without
    h-entries

    """
    activity = self.activities[0]
    activity['object']['content'] = 'post content without backlink'
    activity['object']['url'] = 'http://fa.ke/post/url'

    # silo domain is fa.ke
    source = self.sources[0]
    source.domain_url = 'http://author'

    self.mox.StubOutWithMock(requests, 'get')

    resp = requests.Response()
    resp.status_code = 200
    resp._content = """
    <html class="h-feed">
    <p>under construction</p>
    </html>"""
    requests.get('http://author', timeout=HTTP_TIMEOUT).AndReturn(resp)

    self.mox.ReplayAll()
    logging.debug("Original post discovery %s -> %s", source, activity)
    original_post_discovery.discover(source, activity)

    self.assert_equals(
      [(None, 'http://fa.ke/post/url')],
      [(relationship.original, relationship.syndication)
       for relationship in SyndicatedPost.query(ancestor=source.key)])

  def test_existing_syndicated_posts(self):
    """Confirm that no additional requests are made if we already have a
    SyndicatedPost in the DB.

    """
    original_url = 'http://author/notes/2014/04/24/1'
    syndication_url = 'http://fa.ke/post/url'

    source = self.sources[0]
    source.domain_url = 'http://author'
    activity = self.activities[0]
    activity['object']['url'] = syndication_url
    activity['object']['content'] = 'content without links'

    # save the syndicated post ahead of time (as if it had been
    # discovered previously)
    SyndicatedPost(parent=source.key, original=original_url,
                   syndication=syndication_url).put()

    self.mox.StubOutWithMock(requests, 'get')
    self.mox.ReplayAll()

    logging.debug("Original post discovery %s -> %s", source, activity)
    original_post_discovery.discover(source, activity)

    # should append the author note url, with no addt'l requests
    self.assert_equals([None, None, original_url],
                       [tag.get('url') for tag in activity['object']['tags']])

  def test_invalid_webmention_target(self):
    """Confirm that no additional requests are made if the author url is
    an invalid webmention target. Right now this pretty much just
    means they're on the blacklist. Eventually we want to filter out
    targets that don't have certain features, like a webmention
    endpoint or microformats.

    """

    source = self.sources[0]
    source.domain_url = 'http://amazon.com'
    activity = self.activities[0]
    activity['object']['url'] = 'http://fa.ke/post/url'
    activity['object']['content'] = 'content without links'

    self.mox.StubOutWithMock(requests, 'get')
    self.mox.ReplayAll()

    logging.debug("Original post discovery %s -> %s", source, activity)
    original_post_discovery.discover(source, activity)

    # nothing attempted, but we should have saved a placeholder to prevent us
    # from trying again
    self.assert_equals(
      [(None, 'http://fa.ke/post/url')],
      [(relationship.original, relationship.syndication)
       for relationship in SyndicatedPost.query(ancestor=source.key)])

  def test_failed_domain_url_fetch(self):
    """Make sure something reasonable happens when the author's domain url
    gives an unexpected response

    """
    source = self.sources[0]
    source.domain_url = 'http://author'
    activity = self.activities[0]
    activity['object']['url'] = 'http://fa.ke/post/url'
    activity['object']['content'] = 'content without links'

    self.mox.StubOutWithMock(requests, 'get')
    response = requests.Response()
    response.status_code = 404
    requests.get('http://author', timeout=HTTP_TIMEOUT).AndReturn(response)

    self.mox.ReplayAll()
    original_post_discovery.discover(source, activity)

    # nothing attempted, but we should have saved a placeholder to prevent us
    # from trying again
    self.assert_equals(
      [(None, 'http://fa.ke/post/url')],
      [(relationship.original, relationship.syndication)
       for relationship in SyndicatedPost.query(ancestor=source.key)])

  def test_failed_post_permalink_fetch(self):
    """Make sure something reasonable happens when we're unable to fetch
    the permalink of an entry linked in the h-feed

    """
    source = self.sources[0]
    source.domain_url = 'http://author'
    activity = self.activities[0]
    activity['object']['url'] = 'http://fa.ke/post/url'
    activity['object']['content'] = 'content without links'

    self.mox.StubOutWithMock(requests, 'get')
    response = requests.Response()
    response.status_code = 200
    response._content = """
    <html class="h-feed">
      <article class="h-entry">
        <a class="u-url" href="nonexistent.html"></a>
      </article>
    </html>
    """
    requests.get('http://author', timeout=HTTP_TIMEOUT).AndReturn(response)

    response = requests.Response()
    response.status_code = 410
    requests.get('http://author/nonexistent.html', timeout=HTTP_TIMEOUT)\
            .AndReturn(response)

    self.mox.ReplayAll()
    original_post_discovery.discover(source, activity)

    # we should have saved placeholders to prevent us from trying the
    # syndication url or permalink again
    self.assert_equals(
      set([('http://author/nonexistent.html', None), (None, 'http://fa.ke/post/url')]),
      set((relationship.original, relationship.syndication)
          for relationship in SyndicatedPost.query(ancestor=source.key)))

  def test_no_author_url(self):
    """Make sure something reasonable happens when the author doesn't have
    a url at all.

    """
    source = self.sources[0]
    source.domain_url = None
    activity = self.activities[0]
    activity['object']['url'] = 'http://fa.ke/post/url'
    activity['object']['content'] = 'content without links'

    self.mox.StubOutWithMock(requests, 'get')
    self.mox.ReplayAll()
    original_post_discovery.discover(source, activity)

    # nothing attempted, and no SyndicatedPost saved
    self.assertFalse(SyndicatedPost.query(ancestor=source.key).get())

  #TODO activity with existing responses, make sure they're merged right
