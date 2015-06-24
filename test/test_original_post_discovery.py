# coding=utf-8
"""Unit tests for original_post_discovery.py
"""
import json
import logging


from google.appengine.ext import ndb
from oauth_dropins import facebook as oauth_facebook
import requests
from requests.exceptions import HTTPError

from facebook import FacebookPage
from models import SyndicatedPost
import util
import original_post_discovery
import tasks
import test_facebook
import testutil


class OriginalPostDiscoveryTest(testutil.ModelsTest):

  def setUp(self):
    super(OriginalPostDiscoveryTest, self).setUp()
    self.source = self.sources[0]
    self.source.domain_urls = ['http://author']

    self.activity = self.activities[0]
    self.activity['object'].update({
      'url': 'https://fa.ke/post/url',  # silo domain is fa.ke
      'content': 'content without links',
      })

  def assert_syndicated_posts(self, *expected):
    self.assertItemsEqual(expected,
                          [(r.original, r.syndication) for r in
                           SyndicatedPost.query(ancestor=self.source.key)])

  def test_single_post(self):
    """Test that original post discovery does the reverse lookup to scan
    author's h-feed for rel=syndication links
    """
    self.activity['object']['upstreamDuplicates'] = ['existing uD']

    self.expect_requests_get('http://author', """
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="http://author/post/permalink"></a>
      </div>
    </html>""")

    # syndicated to two places
    self.expect_requests_get('http://author/post/permalink', """
    <link rel="syndication" href="http://not.real/statuses/postid">
    <link rel="syndication" href="https://fa.ke/post/url">
    <div class="h-entry">
      <a class="u-url" href="http://author/post/permalink"></a>
    </div>""")

    self.mox.ReplayAll()
    logging.debug('Original post discovery %s -> %s', self.source, self.activity)
    original_post_discovery.discover(self.source, self.activity)

    # upstreamDuplicates = 1 original + 1 discovered
    self.assertEquals(['existing uD', 'http://author/post/permalink'],
                      self.activity['object']['upstreamDuplicates'])

    origurls = [r.original for r in SyndicatedPost.query(ancestor=self.source.key)]
    self.assertEquals([u'http://author/post/permalink'], origurls)

    # for now only syndicated posts belonging to this source are stored
    syndurls = list(r.syndication for r
                    in SyndicatedPost.query(ancestor=self.source.key))

    self.assertEquals([u'https://fa.ke/post/url'], syndurls)

  def test_syndication_url_in_hfeed(self):
    """Like test_single_post, but because the syndication URL is given in
    the h-feed we skip fetching the permalink. New behavior as of
    2014-11-08
    """
    self.activity['object']['upstreamDuplicates'] = ['existing uD']

    # silo domain is fa.ke
    self.expect_requests_get('http://author', """
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="http://author/post/permalink"></a>
        <a class="u-syndication" href="http://fa.ke/post/url">
      </div>
    </html>""")

    self.mox.ReplayAll()
    logging.debug('Original post discovery %s -> %s', self.source, self.activity)
    original_post_discovery.discover(self.source, self.activity)

    # upstreamDuplicates = 1 original + 1 discovered
    self.assertEquals(['existing uD', 'http://author/post/permalink'],
                      self.activity['object']['upstreamDuplicates'])

    origurls = [r.original for r in SyndicatedPost.query(ancestor=self.source.key)]
    self.assertEquals([u'http://author/post/permalink'], origurls)

    # for now only syndicated posts belonging to this source are stored
    syndurls = list(r.syndication for r
                    in SyndicatedPost.query(ancestor=self.source.key))

    self.assertEquals([u'https://fa.ke/post/url'], syndurls)

  def test_additional_requests_do_not_require_rework(self):
    """Test that original post discovery fetches and stores all entries up
    front so that it does not have to reparse the author's h-feed for
    every new post. Test that original post discovery does the reverse
    lookup to scan author's h-feed for rel=syndication links
    """
    for idx, activity in enumerate(self.activities):
        activity['object'].update({
          'content': 'post content without backlinks',
          'url': 'https://fa.ke/post/url%d' % (idx + 1),
        })

    author_feed = u"""
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="http://author/post/permalink1"></a>
      </div>
      <div class="h-entry">
        <!-- note the unicode char in this href -->
        <a class="u-url" href="http://author/post/perma✁2"></a>
      </div>
      <div class="h-entry">
        <a class="u-url" href="http://author/post/permalink3"></a>
      </div>
    </html>"""

    self.expect_requests_get('http://author', author_feed)

    # first post is syndicated
    self.expect_requests_get('http://author/post/permalink1', """
    <div class="h-entry">
      <a class="u-url" href="http://author/post/permalink1"></a>
      <a class="u-syndication" href="https://fa.ke/post/url1"></a>
    </div>""").InAnyOrder()

    # second post is syndicated
    self.expect_requests_get(u'http://author/post/perma✁2', u"""
    <div class="h-entry">
      <a class="u-url" href="http://author/post/perma✁2"></a>
      <a class="u-syndication" href="https://fa.ke/post/url2"></a>
    </div>""").InAnyOrder()

    # third post is not syndicated
    self.expect_requests_get('http://author/post/permalink3', """
    <div class="h-entry">
      <a class="u-url" href="http://author/post/permalink3"></a>
    </div>""").InAnyOrder()

    # the second activity lookup should not make any HTTP requests

    # the third activity lookup will fetch the author's h-feed one more time
    self.expect_requests_get('http://author', author_feed).InAnyOrder()

    self.mox.ReplayAll()

    # first activity should trigger all the lookups and storage
    original_post_discovery.discover(self.source, self.activities[0])

    self.assertEquals(['http://author/post/permalink1'],
                      self.activities[0]['object']['upstreamDuplicates'])

    # make sure things are where we want them
    rs = SyndicatedPost.query(
      SyndicatedPost.original == 'http://author/post/permalink1',
      ancestor=self.source.key).fetch()
    self.assertEquals('https://fa.ke/post/url1', rs[0].syndication)
    rs = SyndicatedPost.query(
      SyndicatedPost.syndication == 'https://fa.ke/post/url1',
      ancestor=self.source.key).fetch()
    self.assertEquals('http://author/post/permalink1', rs[0].original)

    rs = SyndicatedPost.query(
      SyndicatedPost.original == u'http://author/post/perma✁2',
      ancestor=self.source.key).fetch()
    self.assertEquals('https://fa.ke/post/url2', rs[0].syndication)
    rs = SyndicatedPost.query(
      SyndicatedPost.syndication == 'https://fa.ke/post/url2',
      ancestor=self.source.key).fetch()
    self.assertEquals(u'http://author/post/perma✁2', rs[0].original)

    rs = SyndicatedPost.query(
      SyndicatedPost.original == 'http://author/post/permalink3',
      ancestor=self.source.key).fetch()
    self.assertEquals(None, rs[0].syndication)

    # second lookup should require no additional HTTP requests.
    # the second syndicated post should be linked up to the second permalink.
    original_post_discovery.discover(self.source, self.activities[1])
    self.assertEquals([u'http://author/post/perma✁2'],
                      self.activities[1]['object']['upstreamDuplicates'])

    # third activity lookup.
    # since we didn't find a back-link for the third syndicated post,
    # it should fetch the author's feed again, but seeing no new
    # posts, it should not follow any of the permalinks

    original_post_discovery.discover(self.source, self.activities[2])
    # should have found no new syndication link
    self.assertFalse(self.activities[2]['object'].get('upstreamDuplicates'))

    # should have saved a blank to prevent subsequent checks of this
    # syndicated post from fetching the h-feed again
    rs = SyndicatedPost.query(
      SyndicatedPost.syndication == 'https://fa.ke/post/url3',
      ancestor=self.source.key).fetch()
    self.assertIsNone(rs[0].original)

    # confirm that we do not fetch the h-feed again for the same
    # syndicated post
    original_post_discovery.discover(self.source, self.activities[2])
    # should be no new syndication link
    self.assertFalse(self.activities[2]['object'].get('upstreamDuplicates'))

  def test_no_duplicate_links(self):
    """Make sure that a link found by both original-post-discovery and
    posse-post-discovery will not result in two webmentions being sent.
    """
    self.activity['object']['content'] = 'with a link http://author/post/url'
    original = 'http://author/post/url'

    self.expect_requests_get('http://author', """
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="%s"></a>
      </div>
    </html>""" % original)
    self.expect_requests_get(original, """
    <div class="h-entry">
      <a class="u-url" href="%s"></a>
      <a class="u-syndication" href="%s"></a>
    </div>""" % (original, 'https://fa.ke/post/url'))

    self.mox.ReplayAll()
    logging.debug('Original post discovery %s -> %s', self.source, self.activity)

    wmtargets = tasks.get_webmention_targets(self.source, self.activity)
    self.assertEquals([original], self.activity['object']['upstreamDuplicates'])
    self.assertEquals([original], wmtargets)

  def test_strip_www_when_comparing_domains(self):
    """We should ignore leading www when comparing syndicated URL domains."""
    self.activity['object']['url'] = 'http://www.fa.ke/post/url'

    self.expect_requests_get('http://author', """
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="http://author/post/url"></a>
      </div>
    </html>""")
    self.expect_requests_get('http://author/post/url', """
    <div class="h-entry">
      <a class="u-syndication" href="http://www.fa.ke/post/url"></a>
    </div>""")
    self.mox.ReplayAll()

    original_post_discovery.discover(self.source, self.activity)
    self.assertEquals(['http://author/post/url'],
                      self.activity['object']['upstreamDuplicates'])

  def test_rel_feed_link(self):
    """Check that we follow the rel=feed link when looking for the
    author's full feed URL
    """
    self.expect_requests_get('http://author', """
    <html>
      <head>
        <link rel="feed" type="text/html" href="try_this.html">
        <link rel="alternate" type="application/xml" href="not_this.html">
        <link rel="alternate" type="application/xml" href="nor_this.html">
      </head>
    </html>""")

    self.expect_requests_get('http://author/try_this.html', """
    <html class="h-feed">
      <body>
        <div class="h-entry">Hi</div>
      </body>
    </html>""")

    self.mox.ReplayAll()
    logging.debug('Original post discovery %s -> %s', self.source, self.activity)
    original_post_discovery.discover(self.source, self.activity)

  def test_rel_feed_anchor(self):
    """Check that we follow the rel=feed when it's in an <a> tag instead of <link>
    """
    self.expect_requests_get('http://author', """
    <html>
      <head>
        <link rel="alternate" type="application/xml" href="not_this.html">
        <link rel="alternate" type="application/xml" href="nor_this.html">
      </head>
      <body>
        <a href="try_this.html" rel="feed">full unfiltered feed</a>
      </body>
    </html>""")

    self.expect_requests_get('http://author/try_this.html', """
    <html class="h-feed">
      <body>
        <div class="h-entry">Hi</div>
      </body>
    </html>""")

    self.mox.ReplayAll()
    logging.debug('Original post discovery %s -> %s', self.source, self.activity)
    original_post_discovery.discover(self.source, self.activity)

  def test_no_h_entries(self):
    """Make sure nothing bad happens when fetching a feed without h-entries.
    """
    self.expect_requests_get('http://author', """
    <html class="h-feed">
    <p>under construction</p>
    </html>""")

    self.mox.ReplayAll()
    logging.debug('Original post discovery %s -> %s', self.source, self.activity)
    original_post_discovery.discover(self.source, self.activity)
    self.assert_syndicated_posts((None, 'https://fa.ke/post/url'))

  def test_existing_syndicated_posts(self):
    """Confirm that no additional requests are made if we already have a
    SyndicatedPost in the DB.
    """
    original_url = 'http://author/notes/2014/04/24/1'
    syndication_url = 'https://fa.ke/post/url'

    # save the syndicated post ahead of time (as if it had been
    # discovered previously)
    SyndicatedPost(parent=self.source.key, original=original_url,
                   syndication=syndication_url).put()

    logging.debug('Original post discovery %s -> %s', self.source, self.activity)
    original_post_discovery.discover(self.source, self.activity)

    # should append the author note url, with no addt'l requests
    self.assertEquals([original_url], self.activity['object']['upstreamDuplicates'])

  def test_invalid_webmention_target(self):
    """Confirm that no additional requests are made if the author url is
    an invalid webmention target. Right now this pretty much just
    means they're on the blacklist. Eventually we want to filter out
    targets that don't have certain features, like a webmention
    endpoint or microformats.
    """
    self.source.domain_urls = ['http://amazon.com']

    logging.debug('Original post discovery %s -> %s', self.source, self.activity)
    original_post_discovery.discover(self.source, self.activity)

    # nothing attempted, but we should have saved a placeholder to prevent us
    # from trying again
    self.assert_syndicated_posts((None, 'https://fa.ke/post/url'))

  def _test_failed_domain_url_fetch(self, raise_exception):
    """Make sure something reasonable happens when the author's domain url
    gives an unexpected response
    """
    if raise_exception:
      self.expect_requests_get('http://author').AndRaise(HTTPError())
    else:
      self.expect_requests_get('http://author', status_code=404)

    self.mox.ReplayAll()
    original_post_discovery.discover(self.source, self.activity)

    # nothing attempted, but we should have saved a placeholder to prevent us
    # from trying again
    self.assert_syndicated_posts((None, 'https://fa.ke/post/url'))

  def test_domain_url_not_found(self):
    """Make sure something reasonable happens when the author's domain url
    returns a 404 status code
    """
    self._test_failed_domain_url_fetch(raise_exception=False)

  def test_domain_url_error(self):
    """Make sure something reasonable happens when fetching the author's
    domain url raises an exception
    """
    self._test_failed_domain_url_fetch(raise_exception=True)

  def _expect_multiple_domain_url_fetches(self):
    self.source.domain_urls = ['http://author1', 'http://author2', 'http://author3']
    self.activity['object']['url'] = 'http://fa.ke/A'
    self.expect_requests_get('http://author1', """
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="http://author1/A" />
        <a class="u-syndication" href="http://fa.ke/A" />
      </div>
    </html>""")
    self.expect_requests_get('http://author2').AndRaise(HTTPError())
    self.expect_requests_get('http://author3', """
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="http://author3/B" />
        <a class="u-syndication" href="http://fa.ke/B" />
      </div>
    </html>""")
    self.mox.ReplayAll()

  def test_discover_multiple_domain_urls(self):
    """We should fetch and process all of a source's URLs."""
    self._expect_multiple_domain_url_fetches()
    result = original_post_discovery.discover(self.source, self.activity)
    self.assert_equals(['http://author1/A'], result['object']['upstreamDuplicates'])
    self.assert_syndicated_posts(('http://author1/A', 'https://fa.ke/A'),
                                 ('http://author3/B', 'https://fa.ke/B'))

  def test_refetch_multiple_domain_urls(self):
    """We should refetch all of a source's URLs."""
    self._expect_multiple_domain_url_fetches()
    result = original_post_discovery.refetch(self.source)
    self.assert_equals(['https://fa.ke/A' ,'https://fa.ke/B'], result.keys())
    self.assert_syndicated_posts(('http://author1/A', 'https://fa.ke/A'),
                                 ('http://author3/B', 'https://fa.ke/B'))

  def _test_failed_rel_feed_link_fetch(self, raise_exception):
    """An author page with an invalid rel=feed link. We should recover and
    use any h-entries on the main url as a fallback.
    """
    self.expect_requests_get('http://author', """
    <html>
      <head>
        <link rel="feed" type="text/html" href="try_this.html">
        <link rel="alternate" type="application/xml" href="not_this.html">
        <link rel="alternate" type="application/xml" href="nor_this.html">
      </head>
      <body>
        <div class="h-entry">
          <a class="u-url" href="recover_and_fetch_this.html"></a>
        </div>
      </body>
    </html>""")

    # try to do this and fail
    if raise_exception:
      self.expect_requests_get('http://author/try_this.html').AndRaise(HTTPError())
    else:
      self.expect_requests_get('http://author/try_this.html', status_code=404)

    # despite the error, should fallback on the main page's h-entries and
    # check the permalink
    self.expect_requests_get('http://author/recover_and_fetch_this.html')

    self.mox.ReplayAll()
    logging.debug('Original post discovery %s -> %s', self.source, self.activity)
    original_post_discovery.discover(self.source, self.activity)

  def test_rel_feed_link_not_found(self):
    """Author page has an h-feed link that is 404 not found. We should
    recover and use the main page's h-entries as a fallback."""
    self._test_failed_rel_feed_link_fetch(raise_exception=False)

  def test_rel_feed_link_error(self):
    """Author page has an h-feed link that raises an exception. We should
    recover and use the main page's h-entries as a fallback."""
    self._test_failed_rel_feed_link_fetch(raise_exception=True)

  def _test_failed_post_permalink_fetch(self, raise_exception):
    """Make sure something reasonable happens when we're unable to fetch
    the permalink of an entry linked in the h-feed
    """
    self.expect_requests_get('http://author', """
    <html class="h-feed">
      <article class="h-entry">
        <a class="u-url" href="nonexistent.html"></a>
      </article>
    </html>
    """)

    if raise_exception:
      self.expect_requests_get('http://author/nonexistent.html').AndRaise(HTTPError())
    else:
      self.expect_requests_get('http://author/nonexistent.html', status_code=410)

    self.mox.ReplayAll()
    original_post_discovery.discover(self.source, self.activity)

    # we should have saved placeholders to prevent us from trying the
    # syndication url or permalink again
    self.assert_syndicated_posts(('http://author/nonexistent.html', None),
                                 (None, 'https://fa.ke/post/url'))

  def test_post_permalink_not_found(self):
    """Make sure something reasonable happens when the permalink of an
    entry returns a 404 not found
    """
    self._test_failed_post_permalink_fetch(raise_exception=False)

  def test_post_permalink_error(self):
    """Make sure something reasonable happens when fetching the permalink
    of an entry raises an exception
    """
    self._test_failed_post_permalink_fetch(raise_exception=True)

  def test_no_author_url(self):
    """Make sure something reasonable happens when the author doesn't have
    a url at all.
    """
    self.source.domain_urls = []
    original_post_discovery.discover(self.source, self.activity)
    # nothing attempted, and no SyndicatedPost saved
    self.assertFalse(SyndicatedPost.query(ancestor=self.source.key).get())

  def test_feed_type_application_xml(self):
    """Confirm that we don't follow rel=feeds explicitly marked as
    application/xml.
    """
    self.expect_requests_get('http://author', """
    <html>
      <head>
        <link rel="feed" type="application/xml" href="/updates.atom">
      </head>
    </html>
    """)

    self.mox.ReplayAll()
    original_post_discovery.discover(self.source, self.activity)

  def test_feed_head_request_failed(self):
    """Confirm that we don't follow rel=feeds explicitly marked as
    application/xml.
    """
    self.mox.StubOutWithMock(requests, 'head', use_mock_anything=True)
    self.expect_requests_get('http://author', """
    <html>
      <head>
        <link rel="feed" href="/updates">
      </head>
      <body>
        <article class="h-entry">
          <a class="u-url" href="permalink"></a>
        </article>
      </body>
    </html>
    """)

    # head request to follow redirects on the post url
    self.expect_requests_head(self.activity['object']['url'])

    # and for the author url
    self.expect_requests_head('http://author')

    # try and fail to get the feed
    self.expect_requests_head('http://author/updates', status_code=400)
    self.expect_requests_get('http://author/updates', status_code=400)

    # fall back on the original page, and fetch the post permalink
    self.expect_requests_head('http://author/permalink')
    self.expect_requests_get('http://author/permalink', '<html></html>')

    self.mox.ReplayAll()
    original_post_discovery.discover(self.source, self.activity)

  def test_feed_type_unknown(self):
    """Confirm that we look for an h-feed with type=text/html even when
    the type is not given in <link>, and keep looking until we find one.
    """
    self.mox.StubOutWithMock(requests, 'head', use_mock_anything=True)
    self.expect_requests_get('http://author', """
    <html>
      <head>
        <link rel="feed" href="/updates.atom">
        <link rel="feed" href="/updates.html">
        <link rel="feed" href="/updates.rss">
      </head>
    </html>""")

    # head request to follow redirects on the post url
    self.expect_requests_head(self.activity['object']['url'])

    # and for the author url
    self.expect_requests_head('http://author')

    # try to get the atom feed first
    self.expect_requests_head('http://author/updates.atom',
                              content_type='application/xml')

    # keep looking for an html feed
    self.expect_requests_head('http://author/updates.html')

    # now fetch the html feed
    self.expect_requests_get('http://author/updates.html', """
    <html class="h-feed">
      <article class="h-entry">
        <a class="u-url" href="/permalink">should follow this</a>
      </article>
    </html>""")

    # should not try to get the rss feed at this point
    # but we will follow the post permalink

    # keep looking for an html feed
    self.expect_requests_head('http://author/permalink')
    self.expect_requests_get('http://author/permalink', """
    <html class="h-entry">
      <p class="p-name">Title</p>
    </html>""")

    self.mox.ReplayAll()
    original_post_discovery.discover(self.source, self.activity)

  # TODO: activity with existing responses, make sure they're merged right

  def test_avoid_author_page_with_bad_content_type(self):
    """Confirm that we check the author page's content type before
    fetching and parsing it
    """
    # head request to follow redirects on the post url
    self.expect_requests_head(self.activity['object']['url'])
    self.expect_requests_head('http://author', response_headers={
      'content-type': 'application/xml'
    })

    # give up
    self.mox.ReplayAll()
    original_post_discovery.discover(self.source, self.activity)

  def test_avoid_permalink_with_bad_content_type(self):
    """Confirm that we don't follow u-url's that lead to anything that
    isn't text/html (e.g., PDF)
    """
    # head request to follow redirects on the post url
    self.expect_requests_head(self.activity['object']['url'])
    self.expect_requests_head('http://author')
    self.expect_requests_get('http://author', """
    <html>
      <body>
        <div class="h-entry">
          <a href="http://scholarly.com/paper.pdf">An interesting paper</a>
        </div>
      </body>
    </html>
    """)

    # and to check the content-type of the article
    self.expect_requests_head('http://scholarly.com/paper.pdf',
                              response_headers={
                                'content-type': 'application/pdf'
                              })

    # call to requests.get for permalink should be skipped
    self.mox.ReplayAll()
    original_post_discovery.discover(self.source, self.activity)

  def test_do_not_fetch_hfeed(self):
    """Confirms behavior of discover() when fetch_hfeed=False.
    Discovery should only check the database for previously discovered matches.
    It should not make any GET requests
    """
    original_post_discovery.discover(self.source, self.activity, fetch_hfeed=False)
    self.assertFalse(SyndicatedPost.query(ancestor=self.source.key).get())

  def test_refetch_hfeed(self):
    """refetch should grab resources again, even if they were previously
    marked with a blank SyndicatedPost
    """
    # refetch 1 and 3 to see if they've been updated, 2 has already
    # been resolved for this source
    SyndicatedPost(parent=self.source.key,
                   original='http://author/permalink1',
                   syndication=None).put()

    SyndicatedPost(parent=self.source.key,
                   original='http://author/permalink2',
                   syndication='https://fa.ke/post/url2').put()

    SyndicatedPost(parent=self.source.key,
                   original='http://author/permalink3',
                   syndication=None).put()

    self.expect_requests_get('http://author', """
      <html class="h-feed">
        <a class="h-entry" href="/permalink1"></a>
        <a class="h-entry" href="/permalink2"></a>
        <a class="h-entry" href="/permalink3"></a>
      </html>""")

    # yay, permalink1 has an updated syndication url
    self.expect_requests_get('http://author/permalink1', """
      <html class="h-entry">
        <a class="u-url" href="/permalink1"></a>
        <a class="u-syndication" href="https://fa.ke/post/url1"></a>
      </html>""").InAnyOrder()

    # permalink2 hasn't changed since we first checked it
    self.expect_requests_get('http://author/permalink2', """
      <html class="h-entry">
        <a class="u-url" href="/permalink2"></a>
        <a class="u-syndication" href="https://fa.ke/post/url2"></a>
      </html>""").InAnyOrder()

    # permalink3 hasn't changed since we first checked it
    self.expect_requests_get('http://author/permalink3', """
      <html class="h-entry">
        <a class="u-url" href="/permalink3"></a>
      </html>""").InAnyOrder()

    self.mox.ReplayAll()
    original_post_discovery.refetch(self.source)

    relationships1 = SyndicatedPost.query(
      SyndicatedPost.original == 'http://author/permalink1',
      ancestor=self.source.key).fetch()

    self.assertTrue(relationships1)
    self.assertEquals('https://fa.ke/post/url1', relationships1[0].syndication)

    relationships2 = SyndicatedPost.query(
      SyndicatedPost.original == 'http://author/permalink2',
      ancestor=self.source.key).fetch()

    # this shouldn't have changed
    self.assertTrue(relationships2)
    self.assertEquals('https://fa.ke/post/url2', relationships2[0].syndication)

    relationships3 = SyndicatedPost.query(
      SyndicatedPost.original == 'http://author/permalink3',
      ancestor=self.source.key).fetch()

    self.assertTrue(relationships3)
    self.assertIsNone(relationships3[0].syndication)

  def test_refetch_multiple_responses_same_activity(self):
    """Ensure that refetching a post that has several replies does not
    generate duplicate original -> None blank entries in the
    database. See https://github.com/snarfed/bridgy/issues/259 for
    details
    """
    for activity in self.activities:
        activity['object']['content'] = 'post content without backlinks'
        activity['object']['url'] = 'https://fa.ke/post/url'

    author_feed = """
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="http://author/post/permalink"></a>
      </div>
    </html>"""

    author_entry = """
    <html class="h-entry">
      <a class="u-url" href="http://author/post/permalink"></a>
    </html>"""

    # original
    self.expect_requests_get('http://author', author_feed)
    self.expect_requests_get('http://author/post/permalink', author_entry)
    # refetch
    self.expect_requests_get('http://author', author_feed)
    self.expect_requests_get('http://author/post/permalink', author_entry)
    self.mox.ReplayAll()

    for activity in self.activities:
      original_post_discovery.discover(self.source, activity)

    original_post_discovery.refetch(self.source)

    rels_by_original = list(
      SyndicatedPost.query(SyndicatedPost.original == 'http://author/post/permalink',
                           ancestor=self.source.key).fetch())

    self.assertEquals(1, len(rels_by_original))
    self.assertIsNone(rels_by_original[0].syndication)

    rels_by_syndication = list(
      SyndicatedPost.query(SyndicatedPost.syndication == 'https://fa.ke/post/url',
                           ancestor=self.source.key).fetch())

    self.assertEquals(1, len(rels_by_syndication))
    self.assertIsNone(rels_by_syndication[0].original)

  def test_multiple_refetches(self):
    """Ensure that multiple refetches of the same post (with and without
    u-syndication) does not generate duplicate blank entries in the
    database. See https://github.com/snarfed/bridgy/issues/259 for details
    """
    self.activities[0]['object'].update({
      'content': 'post content without backlinks',
      'url': 'https://fa.ke/post/url',
    })

    hfeed = """<html class="h-feed">
    <a class="h-entry" href="/permalink"></a>
    </html>"""

    unsyndicated = """<html class="h-entry">
    <a class="u-url" href="/permalink"></a>
    </html>"""

    syndicated = """<html class="h-entry">
    <a class="u-url" href="/permalink"></a>
    <a class="u-syndication" href="https://fa.ke/post/url"></a>
    </html>"""

    # first attempt, no syndication url yet
    self.expect_requests_get('http://author', hfeed)
    self.expect_requests_get('http://author/permalink', unsyndicated)

    # refetch, still no syndication url
    self.expect_requests_get('http://author', hfeed)
    self.expect_requests_get('http://author/permalink', unsyndicated)

    # second refetch, has a syndication url this time
    self.expect_requests_get('http://author', hfeed)
    self.expect_requests_get('http://author/permalink', syndicated)

    self.mox.ReplayAll()
    original_post_discovery.discover(self.source, self.activities[0])
    original_post_discovery.refetch(self.source)

    relations = list(
      SyndicatedPost.query(
        SyndicatedPost.original == 'http://author/permalink',
        ancestor=self.source.key).fetch())

    self.assertEquals(1, len(relations))
    self.assertEquals('http://author/permalink', relations[0].original)
    self.assertIsNone(relations[0].syndication)

    original_post_discovery.refetch(self.source)

    relations = list(
      SyndicatedPost.query(
        SyndicatedPost.original == 'http://author/permalink',
        ancestor=self.source.key).fetch())

    self.assertEquals(1, len(relations))
    self.assertEquals('http://author/permalink', relations[0].original)
    self.assertEquals('https://fa.ke/post/url', relations[0].syndication)

  def test_refetch_two_permalinks_same_syndication(self):
    """
    This causes a problem if refetch assumes that syndication-url is
    unique under a given source.
    """
    self.activities[0]['object'].update({
      'content': 'post content without backlinks',
      'url': 'https://fa.ke/post/url',
    })

    hfeed = """<html class="h-feed">
    <a class="h-entry" href="/post1"></a>
    <a class="h-entry" href="/post2"></a>
    </html>"""

    hentries = [
      ('http://author/post%d' % (i + 1),
       """<html class="h-entry">
       <a class="u-url" href="/post%d"></a>
       <a class="u-syndication" href="https://fa.ke/post/url"></a>
       </html>""" % (i + 1)) for i in range(2)
    ]

    self.expect_requests_get('http://author', hfeed)
    for permalink, content in hentries:
      self.expect_requests_get(permalink, content)

    # refetch
    self.expect_requests_get('http://author', hfeed)
    for permalink, content in hentries:
      self.expect_requests_get(permalink, content)

    self.mox.ReplayAll()
    activity = original_post_discovery.discover(self.source, self.activities[0])
    self.assertItemsEqual(['http://author/post1', 'http://author/post2'],
                          activity['object'].get('upstreamDuplicates'))

    self.assert_syndicated_posts(('http://author/post1', 'https://fa.ke/post/url'),
                                 ('http://author/post2', 'https://fa.ke/post/url'))

    # discover should have already handled all relationships, refetch should
    # not find anything
    refetch_result = original_post_discovery.refetch(self.source)
    self.assertFalse(refetch_result)

  def test_refetch_permalink_with_two_syndications(self):
    """Test one permalink with two syndicated posts. Make sure that
    refetch doesn't have a problem with two entries for the same
    original URL.
    """
    for idx, activity in enumerate(self.activities):
      activity['object'].update({
        'content': 'post content without backlinks',
        'url': 'https://fa.ke/post/url%d' % (idx + 1),
      })

    hfeed = """<html class="h-feed">
    <a class="h-entry" href="/permalink"></a>
    </html>"""
    hentry = """<html class="h-entry">
    <a class="u-url" href="/permalink"/>
    <a class="u-syndication" href="https://fa.ke/post/url1"/>
    <a class="u-syndication" href="https://fa.ke/post/url3"/>
    <a class="u-syndication" href="https://fa.ke/post/url5"/>
    </html>"""

    self.expect_requests_get('http://author', hfeed)
    self.expect_requests_get('http://author/permalink', hentry)

    # refetch
    self.expect_requests_get('http://author', hfeed)
    # refetch grabs posts that it's seen before in case there have
    # been updates
    self.expect_requests_get('http://author/permalink', hentry)

    self.mox.ReplayAll()

    original_post_discovery.discover(self.source, self.activities[0])
    relations = SyndicatedPost.query(
      SyndicatedPost.original == 'http://author/permalink',
      ancestor=self.source.key).fetch()
    self.assertItemsEqual(
      [('http://author/permalink', 'https://fa.ke/post/url1'),
       ('http://author/permalink', 'https://fa.ke/post/url3'),
       ('http://author/permalink', 'https://fa.ke/post/url5')],
      [(r.original, r.syndication) for r in relations])

    results = original_post_discovery.refetch(self.source)
    self.assertFalse(results)

  def test_refetch_with_updated_permalink(self):
    """Permalinks can change (e.g., if a stub is added or modified).

    This causes a problem if refetch assumes that syndication-url is
    unique under a given source.
    """
    self.activities[0]['object'].update({
      'content': 'post content without backlinks',
      'url': 'https://fa.ke/post/url',
    })

    # first attempt, no stub yet
    self.expect_requests_get('http://author', """
    <html class="h-feed">
    <a class="h-entry" href="/2014/08/09"></a>
    </html>""")
    self.expect_requests_get('http://author/2014/08/09', """
    <html class="h-entry">
    <a class="u-url" href="/2014/08/09"></a>
    <a class="u-syndication" href="https://fa.ke/post/url"></a>
    </html>""")

    # refetch, permalink has a stub now
    self.expect_requests_get('http://author', """
    <html class="h-feed">
    <a class="h-entry" href="/2014/08/09/this-is-a-stub"></a>
    </html>""")

    self.expect_requests_get('http://author/2014/08/09/this-is-a-stub', """
    <html class="h-entry">
    <a class="u-url" href="/2014/08/09/this-is-a-stub"></a>
    <a class="u-syndication" href="https://fa.ke/post/url"></a>
    </html>""")

    # refetch again
    self.expect_requests_get('http://author', """
    <html class="h-feed">
    <a class="h-entry" href="/2014/08/09/this-is-a-stub"></a>
    </html>""")

    # permalink hasn't changed
    self.expect_requests_get('http://author/2014/08/09/this-is-a-stub', """
    <html class="h-entry">
    <a class="u-url" href="/2014/08/09/this-is-a-stub"></a>
    <a class="u-syndication" href="https://fa.ke/post/url"></a>
    </html>""")

    self.mox.ReplayAll()
    activity = original_post_discovery.discover(self.source, self.activities[0])

    # modified activity should have /2014/08/09 as an upstreamDuplicate now
    self.assertEquals(['http://author/2014/08/09'],
                      activity['object']['upstreamDuplicates'])

    # refetch should find the updated original url -> syndication url.
    # it should *not* find the previously discovered relationship.
    first_results = original_post_discovery.refetch(self.source)
    self.assertEquals(1, len(first_results))
    new_relations = first_results.get('https://fa.ke/post/url')
    self.assertEquals(1, len(new_relations))
    self.assertEquals('https://fa.ke/post/url', new_relations[0].syndication)
    self.assertEquals('http://author/2014/08/09/this-is-a-stub',
                      new_relations[0].original)

    # second refetch should find nothing because nothing has changed
    # since the previous refetch.
    second_results = original_post_discovery.refetch(self.source)
    self.assertFalse(second_results)

  def test_refetch_changed_syndication(self):
    """Update syndication links that have changed since our last fetch."""
    SyndicatedPost(parent=self.source.key,
                   original='http://author/permalink',
                   syndication='https://fa.ke/post/url').put()
    self.expect_requests_get('http://author', """
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="/permalink" />
        <a class="u-syndication" href="http://fa.ke/changed/url" />
      </div>
    </html>""")

    self.mox.ReplayAll()
    results = original_post_discovery.refetch(self.source)
    self.assert_syndicated_posts(('http://author/permalink',
                                  'https://fa.ke/changed/url'))
    self.assert_equals({'https://fa.ke/changed/url': list(SyndicatedPost.query())},
                       results)

  def test_refetch_deleted_syndication(self):
    """Deleted syndication links that have disappeared since our last fetch."""
    SyndicatedPost(parent=self.source.key,
                   original='http://author/permalink',
                   syndication='https://fa.ke/post/url').put()
    self.expect_requests_get('http://author', """
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="/permalink" />
      </div>
    </html>""")
    self.expect_requests_get('http://author/permalink', """
      <html class="h-entry">
        <a class="u-url" href="/permalink"></a>
      </html>""")

    self.mox.ReplayAll()
    self.assert_equals({}, original_post_discovery.refetch(self.source))
    self.assert_syndicated_posts(('http://author/permalink', None))

  def test_refetch_blank_syndication(self):
    """We should preserve blank SyndicatedPosts during refetches."""
    blank = SyndicatedPost(parent=self.source.key,
                           original='http://author/permalink',
                           syndication=None)
    blank.put()
    self.expect_requests_get('http://author', """
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="/permalink" />
      </div>
    </html>""")
    self.expect_requests_get('http://author/permalink', """
      <html class="h-entry">
        <a class="u-url" href="/permalink"></a>
      </html>""")

    self.mox.ReplayAll()
    self.assert_equals({}, original_post_discovery.refetch(self.source))
    self.assert_syndicated_posts(('http://author/permalink', None))
    self.assert_entities_equal([blank], list(SyndicatedPost.query()))

  def test_refetch_unchanged_syndication(self):
    """We should preserve unchanged SyndicatedPosts during refetches."""
    synd = SyndicatedPost(parent=self.source.key,
                          original='http://author/permalink',
                          syndication='https://fa.ke/post/url')
    synd.put()
    self.expect_requests_get('http://author', """
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="/permalink" />
        <a class="u-syndication" href="https://fa.ke/post/url" />
      </div>
    </html>""")

    self.mox.ReplayAll()
    original_post_discovery.refetch(self.source)
    self.assert_entities_equal([synd], list(SyndicatedPost.query()))

  def test_malformed_url_property(self):
    """Non string-like url values (i.e. dicts) used to cause an unhashable
    type exception while processing the h-feed. Make sure that we
    ignore them.
    """
    self.activities[0]['object'].update({
      'content': 'post content without backlinks',
      'url': 'https://fa.ke/post/url',
    })

    # malformed u-url, should skip it without an unhashable dict error
    self.expect_requests_get('http://author', """
<html class="h-feed">
  <div class="h-entry">
    <a class="u-url h-cite" href="/permalink">this is a strange permalink</a>
  </div>
</html>""")

    self.mox.ReplayAll()
    activity = original_post_discovery.discover(self.source, self.activities[0])
    self.assertFalse(activity['object'].get('upstreamDuplicates'))

  def test_merge_front_page_and_h_feed(self):
    """Make sure we are correctly merging the front page and rel-feed by
    checking that we visit h-entries that are only the front page or
    only the rel-feed page.
    """
    self.activity['upstreamDuplicates'] = ['existing uD']

    # silo domain is fa.ke
    self.expect_requests_get('http://author', """
    <link rel="feed" href="/feed">
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="http://author/only-on-frontpage"></a>
      </div>
      <div class="h-entry">
        <a class="u-url" href="http://author/on-both"></a>
      </div>
    </html>""")

    self.expect_requests_get('http://author/feed', """
    <link rel="feed" href="/feed">
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="http://author/on-both"></a>
      </div>
      <div class="h-entry">
        <a class="u-url" href="http://author/only-on-feed"></a>
      </div>
    </html>""")

    for orig in ('/only-on-frontpage', '/on-both', '/only-on-feed'):
      self.expect_requests_get('http://author%s' % orig,
                               """<div class="h-entry">
                                 <a class="u-url" href="%s"></a>
                               </div>""" % orig).InAnyOrder()

    self.mox.ReplayAll()
    logging.debug('Original post discovery %s -> %s', self.source, self.activity)
    original_post_discovery.discover(self.source, self.activity)

    # should be three blank SyndicatedPosts now
    for orig in ('http://author/only-on-frontpage',
                 'http://author/on-both',
                 'http://author/only-on-feed'):
      logging.debug('checking %s', orig)
      sp = SyndicatedPost.query(
        SyndicatedPost.original == orig,
        ancestor=self.source.key).get()
      self.assertTrue(sp)
      self.assertIsNone(sp.syndication)

  def test_match_facebook_username_url(self):
    """Facebook URLs use username and user id interchangeably, and one
    does not redirect to the other. Make sure we can still find the
    relationship if author's publish syndication links using their
    username
    """
    auth_entity = oauth_facebook.FacebookAuth(
      id='my_string_id', auth_code='my_code', access_token_str='my_token',
      user_json=json.dumps({'id': '212038', 'username': 'snarfed.org'}))
    auth_entity.put()

    source = FacebookPage.new(self.handler, auth_entity=auth_entity,
                              domain_urls=['http://author'])
    # facebook activity comes to us with the numeric id
    self.activity['object']['url'] = 'http://facebook.com/212038/posts/314159'

    self.expect_requests_get('http://author', """
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="http://author/post/permalink"></a>
      </div>
    </html>""")

    # user sensibly publishes syndication link using their username
    self.expect_requests_get('http://author/post/permalink', """
    <html class="h-entry">
      <a class="u-url" href="http://author/post/permalink"></a>
      <a class="u-syndication" href="http://facebook.com/snarfed.org/posts/314159"></a>
    </html>""")

    self.mox.ReplayAll()
    original_post_discovery.discover(source, self.activity)

    self.assertEquals(['http://author/post/permalink'],
                      self.activity['object']['upstreamDuplicates'])
