# coding=utf-8
"""Unit tests for original_post_discovery.py
"""
from datetime import datetime, timezone
from string import hexdigits

from oauth_dropins.webutil.testutil import NOW
from oauth_dropins.webutil.util import json_dumps, json_loads
from requests.exceptions import HTTPError

from github import GitHub
from models import SyndicatedPost
import original_post_discovery
from original_post_discovery import (
  discover,
  refetch,
  MAX_ORIGINAL_CANDIDATES,
  MAX_MENTION_CANDIDATES,
)
from . import testutil
import util


class OriginalPostDiscoveryTest(testutil.AppTest):

  def setUp(self):
    super().setUp()
    self.source = self.sources[0]
    self.source.domain_urls = ['http://author/']
    self.source.domains = ['author']
    self.source.put()
    self.source.updates = {}

    self.activity = self.activities[0]
    self.activity['object'].update({
      'url': 'https://fa.ke/post/url',  # silo domain is fa.ke
      'content': 'content without links',
      })

  def assert_discover(self, expected_originals, expected_mentions=[], **kwargs):
    got = discover(self.source, self.activity, **kwargs)
    self.assertEqual((set(expected_originals), set(expected_mentions)), got, got)

  def assert_syndicated_posts(self, *expected):
    got = [(r.original, r.syndication) for r in
           SyndicatedPost.query(ancestor=self.source.key)]
    self.assertCountEqual(expected, got, got)

  def test_single_post(self):
    """Test that original post discovery does the reverse lookup to scan
    author's h-feed for rel=syndication links
    """
    self.expect_requests_get('http://author/', """
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
    self.assertIsNone(self.source.last_syndication_url)
    self.assert_discover(['http://author/post/permalink'])
    self.assert_syndicated_posts(('http://author/post/permalink',
                                  'https://fa.ke/post/url'))
    self.assertEqual(NOW, self.source.updates['last_syndication_url'])

  def test_syndication_url_in_hfeed(self):
    """Like test_single_post, but because the syndication URL is given in
    the h-feed we skip fetching the permalink.
    """
    # silo domain is fa.ke
    self.expect_requests_get('http://author/', """
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="http://author/post/permalink"></a>
        <a class="u-syndication" href="http://fa.ke/post/url"></a>
      </div>
    </html>""")

    self.mox.ReplayAll()
    self.assert_discover(['http://author/post/permalink'])
    self.assert_syndicated_posts(('http://author/post/permalink',
                                  'https://fa.ke/post/url'))

    self.assertEqual(NOW, self.source.updates['last_syndication_url'])
    self.assertEqual(NOW, self.source.updates['last_feed_syndication_url'])

  def test_syndication_url_in_hfeed_with_redirect(self):
    """Like test_syndication_url_in_hfeed but u-url redirects to the
    actual post URL. We should follow the redirect like we do everywhere
    else.
    """
    self.expect_requests_head('https://fa.ke/post/url')
    self.expect_requests_head('http://author/')
    self.expect_requests_get('http://author/', """
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="http://author/post/will-redirect"></a>
        <a class="u-syndication" href="https://fa.ke/post/url"></a>
      </div>
    </html>""")

    self.expect_requests_head(
      'http://author/post/will-redirect',
      redirected_url='http://author/post/final')

    self.mox.ReplayAll()
    self.assert_discover(['http://author/post/final'])
    self.assert_syndicated_posts(('http://author/post/final',
                                  'https://fa.ke/post/url'))

  def test_nested_hfeed(self):
    """Test that we find an h-feed nested inside an h-card like on tantek.com"""
    self.expect_requests_get('http://author/', """
    <html class="h-card">
      <span class="p-name">Author</span>
      <div class="h-feed">
        <div class="h-entry">
          <a class="u-url" href="http://author/post/permalink"></a>
        </div>
      </div>
    </html>
    """)

    self.expect_requests_get('http://author/post/permalink', """
    <html class="h-entry">
      <a class="u-url" href="http://author/post/permalink"></a>
      <a class="u-syndication" href="https://fa.ke/post/url"></a>
    </html>
    """)

    self.mox.ReplayAll()
    self.assert_discover(['http://author/post/permalink'])
    self.assert_syndicated_posts(('http://author/post/permalink',
                                  'https://fa.ke/post/url'))

  def test_multiple_hfeeds(self):
    """That that we search all the h-feeds on a page if there are more than one.

    Inspired by https://sixtwothree.org/
    """
    for i, activity in enumerate(self.activities):
      activity['object'].update({
        'content': 'post content without backlinks',
        'url': f'https://fa.ke/post/url{i + 1}',
      })

    # silo domain is fa.ke
    self.expect_requests_get('http://author/', """
    <html>
      <div class="h-feed">
        <div class="h-entry">
          <a class="u-url" href="http://author/post/permalink1"></a>
          <a class="u-syndication" href="http://fa.ke/post/url1"></a>
        </div>
      </div>
      <div class="h-feed">
        <div class="h-entry">
          <a class="u-url" href="http://author/post/permalink2"></a>
          <a class="u-syndication" href="http://fa.ke/post/url2"></a>
        </div>
      </div>
    </html>""")

    self.mox.ReplayAll()
    self.assert_discover(['http://author/post/permalink1'])
    self.assert_syndicated_posts(
      ('http://author/post/permalink1', 'https://fa.ke/post/url1'),
      ('http://author/post/permalink2', 'https://fa.ke/post/url2'),
    )

  def test_additional_requests_do_not_require_rework(self):
    """Test that original post discovery fetches and stores all entries up
    front so that it does not have to reparse the author's h-feed for
    every new post. Test that original post discovery does the reverse
    lookup to scan author's h-feed for rel=syndication links
    """
    for i, activity in enumerate(self.activities):
      activity['object'].update({
        'content': 'post content without backlinks',
        'url': f'https://fa.ke/post/url{i + 1}',
      })

    author_feed = u"""
    <html class="h-feed">
      <head><meta charset="utf-8"></head>
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

    self.expect_requests_get('http://author/', author_feed)

    # first post is syndicated
    self.expect_requests_get('http://author/post/permalink1', """
    <div class="h-entry">
      <a class="u-url" href="http://author/post/permalink1"></a>
      <a class="u-syndication" href="https://fa.ke/post/url1"></a>
    </div>""").InAnyOrder()

    # second post is syndicated
    self.expect_requests_get('http://author/post/perma✁2', u"""
    <div class="h-entry">
      <a class="u-url" href="http://author/post/perma✁2"></a>
      <a class="u-syndication" href="https://fa.ke/post/url2"></a>
    </div>""", content_type='text/html; charset=utf-8').InAnyOrder()

    # third post is not syndicated
    self.expect_requests_get('http://author/post/permalink3', """
    <div class="h-entry">
      <a class="u-url" href="http://author/post/permalink3"></a>
    </div>""").InAnyOrder()

    # the second activity lookup should not make any HTTP requests

    # the third activity lookup will fetch the author's h-feed one more time
    self.expect_requests_get('http://author/', author_feed).InAnyOrder()

    self.mox.ReplayAll()

    # first activity should trigger all the lookups and storage
    self.assert_discover(['http://author/post/permalink1'])
    syndposts = [('http://author/post/permalink1', 'https://fa.ke/post/url1'),
                 ('http://author/post/perma✁2', 'https://fa.ke/post/url2'),
                 ('http://author/post/permalink3', None)]
    self.assert_syndicated_posts(*syndposts)

    # second lookup should require no additional HTTP requests.
    # the second syndicated post should be linked up to the second permalink.
    self.assertEqual((set(['http://author/post/perma✁2']), set()),
                      discover(self.source, self.activities[1]))

    # third activity lookup. since we didn't find a back-link for the third
    # syndicated post, it should fetch the author's feed again, but seeing no
    # new posts, it should not follow any of the permalinks.
    self.assertEqual((set(), set()), discover(self.source, self.activities[2]))

    # should have saved a blank to prevent subsequent checks of this syndicated
    # post from fetching the h-feed again
    syndposts.append((None, 'https://fa.ke/post/url3'))
    self.assert_syndicated_posts(*syndposts)

    # confirm that we do not fetch the h-feed again for the same syndicated post
    self.assertEqual((set(), set()), discover(self.source, self.activities[2]))

  def test_no_duplicate_links(self):
    """Make sure that a link found by both original-post-discovery and
    posse-post-discovery will not result in two webmentions being sent.
    """
    self.activity['object']['content'] = 'with a link http://author/post/url'
    original = 'http://author/post/url'

    self.expect_requests_get('http://author/', f"""
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="{original}"></a>
      </div>
    </html>""")
    self.expect_requests_get(original, f"""
    <div class="h-entry">
      <a class="u-url" href="{original}"></a>
      <a class="u-syndication" href="{'https://fa.ke/post/url'}"></a>
    </div>""")

    self.mox.ReplayAll()
    self.assert_discover([original])

  def test_exclude_mentions_except_user(self):
    """Ignore mentions *except* to the user themselves."""
    self.activity['object'].update({
      'content': 'foo http://author/ bar http://other/',
      'tags': [{
        'objectType': 'person',
        'url': 'http://author/',
      }, {
        'objectType': 'person',
        'url': 'http://other/',
      }],
    })
    self.assert_discover(['http://author/'], fetch_hfeed=False)

  def test_require_http_or_https(self):
    """Ignore non-http URLs."""
    self.activity['object']['content'] = 'ftp://a/b chrome://flags dat://c/d'
    self.assert_discover([], fetch_hfeed=False)

  def test_strip_www_when_comparing_domains(self):
    """We should ignore leading www when comparing syndicated URL domains."""
    self.activity['object']['url'] = 'http://www.fa.ke/post/url'

    self.expect_requests_get('http://author/', """
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
    self.assert_discover(['http://author/post/url'])

  def test_ignore_synd_urls_on_other_silos(self):
    """We should ignore syndication URLs on other (silos') domains."""
    self.expect_requests_get('http://author/', """
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="http://author/post/url"></a>
        <a class="u-syndication" href="http://other/silo/url"></a>
      </div>
    </html>""")
    self.expect_requests_get('http://author/post/url')

    self.mox.ReplayAll()
    self.assert_discover([])
    self.assert_syndicated_posts(('http://author/post/url', None),
                                 (None, 'https://fa.ke/post/url'))

  def test_rel_feed_alternate_links(self):
    """Check that we follow rel=feed and rel=alternate type=mf2+html links."""
    self.expect_requests_get('http://author/', """
    <html>
      <head>
        <link rel="feed" type="text/html" href="try_this.html">
        <link rel="alternate" type="application/xml" href="not_this.html">
        <link rel="alternate" type="text/mf2+html" href="and_this.html">
        <link rel="alternate" type="application/xml" href="nor_this.html">
      </head>
    </html>""")

    html = """\
    <html class="h-feed">
      <body>
        <div class="h-entry">Hi</div>
      </body>
    </html>"""
    self.expect_requests_get('http://author/try_this.html', html).InAnyOrder()
    self.expect_requests_get('http://author/and_this.html', html).InAnyOrder()

    self.mox.ReplayAll()
    discover(self.source, self.activity)

  def test_rel_feed_anchor(self):
    """Check that we follow the rel=feed when it's in an <a> tag instead of <link>
    """
    self.expect_requests_get('http://author/', """
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
    discover(self.source, self.activity)

  def test_rel_feed_adds_to_domains(self):
    """rel=feed discovery should update Source.domains."""
    self.expect_requests_get('http://author/', """
    <html>
      <head>
        <link rel="feed" type="text/html" href="http://other/domain">
      </head>
    </html>""")
    self.expect_requests_get('http://other/domain', 'foo')
    self.mox.ReplayAll()

    discover(self.source, self.activity)
    self.assertEqual(['author', 'other'], self.source.updates['domains'])

  def test_no_h_entries(self):
    """Make sure nothing bad happens when fetching a feed without h-entries."""
    self.expect_requests_get('http://author/', """
    <html class="h-feed">
    <p>under construction</p>
    </html>""")

    self.mox.ReplayAll()
    self.assert_discover([])
    self.assert_syndicated_posts((None, 'https://fa.ke/post/url'))

  def test_fragment_not_found(self):
    """Make sure nothing bad happens when fetching a feed without h-entries."""
    self.source.domain_urls = ['http://author/#nope']
    self.source.put()

    self.expect_requests_get('http://author/', """
    <html class="h-feed">
    <p>under construction</p>
    </html>""")

    self.mox.ReplayAll()
    self.assert_discover([])
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

    # should append the author note url, with no addt'l requests
    self.assert_discover([original_url])

  def test_invalid_webmention_target(self):
    """Confirm that no additional requests are made if the author url is
    an invalid webmention target. Right now this pretty much just
    means they're on the blocklist. Eventually we want to filter out
    targets that don't have certain features, like a webmention
    endpoint or microformats.
    """
    self.source.domain_urls = ['http://amazon.com']
    discover(self.source, self.activity)
    # nothing attempted, but we should have saved a placeholder to prevent us
    # from trying again
    self.assert_syndicated_posts((None, 'https://fa.ke/post/url'))

  def _test_failed_domain_url_fetch(self, raise_exception):
    """Make sure something reasonable happens when the author's domain url
    gives an unexpected response
    """
    if raise_exception:
      self.expect_requests_get('http://author/').AndRaise(HTTPError())
    else:
      self.expect_requests_get('http://author/', status_code=404)

    self.mox.ReplayAll()
    discover(self.source, self.activity)

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
        <a class="u-url" href="http://author1/A"></a>
        <a class="u-syndication" href="http://fa.ke/A"></a>
      </div>
    </html>""")
    self.expect_requests_get('http://author2').AndRaise(HTTPError())
    self.expect_requests_get('http://author3', """
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="http://author3/B"></a>
        <a class="u-syndication" href="http://fa.ke/B"></a>
      </div>
    </html>""")
    self.mox.ReplayAll()

  def test_canonicalize_drops_non_silo_activity_url(self):
    """For https://console.cloud.google.com/errors/CNnLpJml7O3cvAE ."""
    self.source.BACKFEED_REQUIRES_SYNDICATION_LINK = True
    self.activity['object']['url'] = 'http://not/silo'
    self.assert_discover([])

  def test_discover_multiple_domain_urls(self):
    """We should fetch and process all of a source's URLs."""
    self._expect_multiple_domain_url_fetches()
    self.assert_discover(['http://author1/A'])
    self.assert_syndicated_posts(('http://author1/A', 'https://fa.ke/A'),
                                 ('http://author3/B', 'https://fa.ke/B'))

  def test_refetch_multiple_domain_urls(self):
    """We should refetch all of a source's URLs."""
    self._expect_multiple_domain_url_fetches()
    result = refetch(self.source)
    self.assert_equals(['https://fa.ke/A' ,'https://fa.ke/B'], list(result.keys()))
    self.assert_syndicated_posts(('http://author1/A', 'https://fa.ke/A'),
                                 ('http://author3/B', 'https://fa.ke/B'))

  def test_url_limit(self):
    """We should cap fetches at 5 URLs."""
    self.source.domain_urls = ['http://a1', 'http://b2', 'https://c3',
                               'http://d4', 'http://e5', 'https://f6']
    for url in self.source.domain_urls[:5]:
      self.expect_requests_get(url, '')
    self.mox.ReplayAll()
    self.assert_discover([])

  def test_permalink_limit(self):
    self.mox.stubs.Set(original_post_discovery, 'MAX_PERMALINK_FETCHES_BETA', 3)

    self.expect_requests_get('http://author/', """
<html><body>
<div class="h-feed first">
  <div class="h-entry"><a class="u-url" href="http://author/a"></a></div>
  <div class="h-entry"><a class="u-url" href="http://author/b"></a></div>
  <div class="h-entry">
    <a class="u-url" href="http://author/c"></a>
    <time class="dt-published" datetime="2016-01-03T00:00:00-00:00">
  </div>
</div>
<div class="h-feed first">
  <div class="h-entry"><a class="u-url" href="http://author/d"></a></div>
  <div class="h-entry">
    <a class="u-url" href="http://author/e"></a>
    <time class="dt-published" datetime="2016-01-02T00:00:00-00:00">
  </div>
  <div class="h-entry"><a class="u-url" href="http://author/f"></a></div>
</div>
</body></html>""")

    # should sort by dt-updated/dt-published, then feed order
    self.expect_requests_get('http://author/c')
    self.expect_requests_get('http://author/e')
    self.expect_requests_get('http://author/a')

    self.mox.ReplayAll()
    self.assert_discover([])

  def test_feed_entry_limit(self):
    self.mox.stubs.Set(original_post_discovery, 'MAX_FEED_ENTRIES', 2)

    self.expect_requests_get('http://author/', """
<html><body>
<div class="h-feed">
  <div class="h-entry"><a class="u-url" href="http://author/a"></a>
    <a class="u-syndication" href="http://fa.ke/post/url"></a></div>
  <div class="h-entry"><a class="u-url" href="http://author/b"></a>
    <a class="u-syndication" href="http://fa.ke/post/url"></a></div>
  <div class="h-entry"><a class="u-url" href="http://author/c"></a>
    <a class="u-syndication" href="http://fa.ke/post/url"></a></div>
  <div class="h-entry"><a class="u-url" href="http://author/d"></a>
    <a class="u-syndication" href="http://fa.ke/post/url"></a></div>
</body></html>""")

    self.mox.ReplayAll()
    self.assert_discover(['http://author/a', 'http://author/b'])
    self.assert_syndicated_posts(('http://author/a', 'https://fa.ke/post/url'),
                                 ('http://author/b', 'https://fa.ke/post/url'))

  def test_homepage_too_big(self):
    self.expect_requests_head('https://fa.ke/post/url')
    self.expect_requests_head('http://author/',
      response_headers={'Content-Length': str(util.MAX_HTTP_RESPONSE_SIZE + 1)})
    # no GET for /author since it's too big
    self.mox.ReplayAll()
    self.assert_discover([])

  def test_feed_too_big(self):
    self.expect_requests_head('https://fa.ke/post/url')
    self.expect_requests_head('http://author/')
    self.expect_requests_get(
      'http://author/',
      '<html><head><link rel="feed" type="text/html" href="/feed"></head></html>')
    self.expect_requests_head('http://author/feed', response_headers={
      'Content-Type': 'text/html',
      'Content-Length': str(util.MAX_HTTP_RESPONSE_SIZE + 1),
    })
    # no GET for /author/feed since it's too big
    self.mox.ReplayAll()
    self.assert_discover([])

  def test_syndication_url_head_error(self):
    """We should ignore syndication URLs that 4xx or 5xx."""
    self.expect_requests_head('https://fa.ke/post/url')
    self.expect_requests_head('http://author/')
    self.expect_requests_get('http://author/', """
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="http://author/post"></a>
        <a class="u-syndication" href="https://fa.ke/other"></a>
      </div>
    </html>""")
    self.expect_requests_head('http://author/post')
    self.expect_requests_get('http://author/post')
    self.expect_requests_head('https://fa.ke/other', status_code=404)
    self.mox.ReplayAll()

    self.assert_discover([])
    self.assert_syndicated_posts(('http://author/post', None),
                                 (None, 'https://fa.ke/post/url'))

  def test_rel_feed_link_error(self):
    """Author page has an h-feed link that raises an exception. We should
    recover and use the main page's h-entries as a fallback."""
    self.expect_requests_get('http://author/', """
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
    self.expect_requests_get('http://author/try_this.html', 'nope',
                             status_code=404)

    # despite the error, should fallback on the main page's h-entries and
    # check the permalink
    self.expect_requests_get('http://author/recover_and_fetch_this.html', 'ok')

    self.mox.ReplayAll()
    discover(self.source, self.activity)

  def _test_failed_post_permalink_fetch(self, raise_exception):
    """Make sure something reasonable happens when we're unable to fetch
    the permalink of an entry linked in the h-feed
    """
    self.expect_requests_get('http://author/', """
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
    discover(self.source, self.activity)
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
    discover(self.source, self.activity)
    # nothing attempted, and no SyndicatedPost saved
    self.assertFalse(SyndicatedPost.query(ancestor=self.source.key).get())

  def test_feed_type_application_xml(self):
    """Confirm that we don't fetch non-HTML rel=feeds.
    """
    self.expect_requests_head(self.activity['object']['url'])
    self.expect_requests_head('http://author/')
    self.expect_requests_get('http://author/', """
    <html>
      <head>
        <link rel="feed" href="/updates.atom">
      </head>
    </html>
    """)
    self.expect_requests_head('http://author/updates.atom',
                              response_headers={'Content-Type': 'application/xml'})
    # check that we don't GET http://author/updates.atom
    self.mox.ReplayAll()
    discover(self.source, self.activity)

  def test_feed_head_request_failed(self):
    """Confirm that we fetch permalinks even if HEAD fails.
    """
    self.expect_requests_get('http://author/', """
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
    self.expect_requests_head('http://author/')

    # try and fail to get the feed
    self.expect_requests_head('http://author/updates', status_code=400)
    self.expect_requests_get('http://author/updates', status_code=400)

    # fall back on the original page, and fetch the post permalink
    self.expect_requests_head('http://author/permalink')
    self.expect_requests_get('http://author/permalink', '<html></html>')

    self.mox.ReplayAll()
    discover(self.source, self.activity)

  def test_feed_type_unknown(self):
    """Confirm that we look for an h-feed with type=text/html even when
    the type is not given in <link>, and keep looking until we find one.
    """
    self.expect_requests_get('http://author/', """
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
    self.expect_requests_head('http://author/')

    # try to get the atom feed first
    self.expect_requests_head('http://author/updates.atom',
                              content_type='application/xml')

    # keep looking for an html feed
    self.expect_requests_head('http://author/updates.html')

    # look at the rss feed last
    self.expect_requests_head('http://author/updates.rss',
                              content_type='application/xml')

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
    discover(self.source, self.activity)

  # TODO: activity with existing responses, make sure they're merged right

  def test_multiple_rel_feeds(self):
    """Make sure that we follow all rel=feed links, e.g. if notes and
    articles are in separate feeds."""

    self.expect_requests_get('http://author/', """
    <html>
      <head>
        <link rel="feed" href="/articles" type="text/html">
        <link rel="feed" href="/notes" type="text/html">
      </head>
    </html>""")

    # fetches all feeds first
    self.expect_requests_get('http://author/articles', """
    <html class="h-feed">
      <article class="h-entry">
        <a class="u-url" href="/article-permalink"></a>
      </article>
    </html>""").InAnyOrder('feed')

    self.expect_requests_get('http://author/notes', """
    <html class="h-feed">
      <article class="h-entry">
        <a class="u-url" href="/note-permalink"></a>
      </article>
    </html>""").InAnyOrder('feed')

    # then the permalinks (in any order since they are hashed to
    # remove duplicates)
    self.expect_requests_get('http://author/article-permalink', """
    <html class="h-entry">
      <a class="u-url" href="/article-permalink"></a>
      <a class="u-syndication" href="https://fa.ke/article"></a>
    </html>""").InAnyOrder('permalink')

    self.expect_requests_get('http://author/note-permalink', """
    <html class="h-entry">
      <a class="u-url" href="/note-permalink"></a>
      <a class="u-syndication" href="https://fa.ke/note"></a>
    </html>""").InAnyOrder('permalink')

    self.mox.ReplayAll()
    discover(self.source, self.activity)
    self.assert_syndicated_posts(
      ('http://author/note-permalink', 'https://fa.ke/note'),
      ('http://author/article-permalink', 'https://fa.ke/article'),
      (None, 'https://fa.ke/post/url'))

  def test_avoid_author_page_with_bad_content_type(self):
    """Confirm that we check the author page's content type before
    fetching and parsing it
    """
    # head request to follow redirects on the post url
    self.expect_requests_head(self.activity['object']['url'])
    self.expect_requests_head('http://author/', response_headers={
      'content-type': 'application/xml',
    })

    # give up
    self.mox.ReplayAll()
    discover(self.source, self.activity)

  def test_avoid_permalink_with_bad_content_type(self):
    """Confirm that we don't follow u-url's that lead to anything that
    isn't text/html (e.g., PDF)
    """
    # head request to follow redirects on the post url
    self.expect_requests_head(self.activity['object']['url'])
    self.expect_requests_head('http://author/')
    self.expect_requests_get('http://author/', """
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
    discover(self.source, self.activity)

  def test_do_not_fetch_hfeed(self):
    """Confirms behavior of discover() when fetch_hfeed=False.
    Discovery should only check the database for previously discovered matches.
    It should not make any GET requests
    """
    discover(self.source, self.activity, fetch_hfeed=False)
    self.assertFalse(SyndicatedPost.query(ancestor=self.source.key).get())

  def test_source_domains(self):
    """Only links to the user's own domains should end up in originals."""
    self.expect_requests_get('http://author/', '')
    self.mox.ReplayAll()

    self.activity['object']['content'] = 'x http://author/post y https://mention z'
    self.assert_discover(['http://author/post'], ['https://mention/'])

    self.activity['object']['content'] = 'a https://mention b'
    self.assert_discover([], ['https://mention/'])

    # if we don't know the user's domains, we should allow anything
    self.source.domain_urls = self.source.domains = []
    self.source.put()

    self.assert_discover(['https://mention/'])

  def test_not_source_DOMAIN(self):
    """Links to the source silo's domain should be ignored."""
    self.source.domain_urls = self.source.domains = []
    self.activity['object']['content'] = 'x http://fa.ke/post'
    self.assert_discover([], [])

  def test_source_user(self):
    """Only links from the user's own posts should end up in originals."""
    self.activity['object']['content'] = 'x http://author/post y'
    self.expect_requests_get('http://author/', '')
    self.mox.ReplayAll()

    user_id = self.source.user_tag_id()
    assert user_id
    self.activity['object']['author'] = {'id': user_id}
    self.assert_discover(['http://author/post'], [])

    self.activity['object']['author'] = {'id': 'tag:fa.ke,2013:someone_else'}
    self.assert_discover([], ['http://author/post'])

    del self.activity['object']['author']
    self.assert_discover(['http://author/post'], [])

  def test_source_user_case_insensitive(self):
    """If USERNAME_KEY_ID, username comparison should ignore case."""
    self.mox.stubs.Set(testutil.FakeSource, 'USERNAME_KEY_ID', True)

    self.source = testutil.FakeSource(
      id='FOO_bar', domain_urls=['http://author/'], domains=['author'])
    self.source.put()

    self.activity['object']['content'] = 'x http://author/post y'
    self.expect_requests_get('http://author/', '')
    self.mox.ReplayAll()

    self.activity['object']['author'] = {'id': 'tag:fa.ke,2013:foo_BAR'}
    self.assert_discover(['http://author/post'], [])

  def test_compare_username(self):
    """Accept posts with author id with the user's username."""
    self.activity['object']['content'] = 'x http://author/post y'
    self.expect_requests_get('http://author/', '')
    self.mox.ReplayAll()

    self.activity['object']['author'] = {
      'id': 'tag:fa.ke,2013:someone_else',
      'username': self.source.key.id(),
    }
    self.assert_discover(['http://author/post'], [])

  def test_attachments(self):
    """Discovery should search for original URL of attachments when the
    attachment is by our user.
    """
    SyndicatedPost(parent=self.source.key,
                   original='http://author/permalink',
                   syndication='https://fa.ke/post/quoted').put()

    self.activity['object']['author'] = {
      'id': 'tag:fa.ke,2013:someone_else',
    }
    self.activity['object']['attachments'] = [{
      'objectType': 'note',
      'content': 'This note is being referenced or otherwise quoted',
      'author': {'id': self.source.user_tag_id()},
      'url': 'https://fa.ke/post/quoted',
    }]

    self.expect_requests_get('http://author/', '')
    self.mox.ReplayAll()

    self.assert_discover([], ['http://author/permalink'])

  def test_max_candidates(self):
    """Check that we cap originals and mentions."""
    origs = [f'http://author/{hexdigits[i]}' for i in range(MAX_ORIGINAL_CANDIDATES + 1)]
    mentions = [f'http://other/{hexdigits[i]}' for i in range(MAX_MENTION_CANDIDATES + 1)]
    self.activity['object']['content'] = f'{" ".join(origs)} {" ".join(mentions)}'

    self.assert_discover(origs[:MAX_ORIGINAL_CANDIDATES],
                         mentions[:MAX_MENTION_CANDIDATES], fetch_hfeed=False)

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
                   original=None,
                   syndication='https://fa.ke/post/url1').put()

    SyndicatedPost(parent=self.source.key,
                   original='http://author/permalink2',
                   syndication='https://fa.ke/post/url2').put()

    SyndicatedPost(parent=self.source.key,
                   original='http://author/permalink3',
                   syndication=None).put()

    self.expect_requests_get('http://author/', """
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
    refetch(self.source)
    self.assert_syndicated_posts(
      ('http://author/permalink1', 'https://fa.ke/post/url1'),
      ('http://author/permalink2', 'https://fa.ke/post/url2'),
      ('http://author/permalink3', None))

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
    self.expect_requests_get('http://author/', author_feed)
    self.expect_requests_get('http://author/post/permalink', author_entry)
    # refetch
    self.expect_requests_get('http://author/', author_feed)
    self.expect_requests_get('http://author/post/permalink', author_entry)
    self.mox.ReplayAll()

    for activity in self.activities:
      discover(self.source, activity)
    refetch(self.source)
    self.assert_syndicated_posts(('http://author/post/permalink', None),
                                 (None, 'https://fa.ke/post/url'))

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
    self.expect_requests_get('http://author/', hfeed)
    self.expect_requests_get('http://author/permalink', unsyndicated)

    # refetch, still no syndication url
    self.expect_requests_get('http://author/', hfeed)
    self.expect_requests_get('http://author/permalink', unsyndicated)

    # second refetch, has a syndication url this time
    self.expect_requests_get('http://author/', hfeed)
    self.expect_requests_get('http://author/permalink', syndicated)

    self.mox.ReplayAll()
    discover(self.source, self.activities[0])
    refetch(self.source)
    self.assert_syndicated_posts(('http://author/permalink', None),
                                 (None, 'https://fa.ke/post/url'))

    refetch(self.source)
    self.assert_syndicated_posts(('http://author/permalink', 'https://fa.ke/post/url'))

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
      (f'http://author/post{i + 1}',
       f"""<html class="h-entry">
       <a class="u-url" href="/post{i + 1}"></a>
       <a class="u-syndication" href="https://fa.ke/post/url"></a>
       </html>""") for i in range(2)
    ]

    self.expect_requests_get('http://author/', hfeed)
    for permalink, content in hentries:
      self.expect_requests_get(permalink, content)

    # refetch
    self.expect_requests_get('http://author/', hfeed)
    for permalink, content in hentries:
      self.expect_requests_get(permalink, content)

    self.mox.ReplayAll()
    self.assert_discover(['http://author/post1', 'http://author/post2'])
    self.assert_syndicated_posts(('http://author/post1', 'https://fa.ke/post/url'),
                                 ('http://author/post2', 'https://fa.ke/post/url'))

    # discover should have already handled all relationships, refetch should
    # not find anything
    self.assertFalse(refetch(self.source))

  def test_refetch_permalink_with_two_syndications(self):
    """Test one permalink with two syndicated posts. Make sure that
    refetch doesn't have a problem with two entries for the same
    original URL.
    """
    for idx, activity in enumerate(self.activities):
      activity['object'].update({
        'content': 'post content without backlinks',
        'url': f'https://fa.ke/post/url{idx + 1}',
      })

    hfeed = """<html class="h-feed">
    <a class="h-entry" href="/permalink"></a>
    </html>"""
    hentry = """<html class="h-entry">
    <a class="u-url" href="/permalink"></a>
    <a class="u-syndication" href="https://fa.ke/post/url1"></a>
    <a class="u-syndication" href="https://fa.ke/post/url3"></a>
    <a class="u-syndication" href="https://fa.ke/post/url5"></a>
    </html>"""

    self.expect_requests_get('http://author/', hfeed)
    self.expect_requests_get('http://author/permalink', hentry)

    # refetch
    self.expect_requests_get('http://author/', hfeed)
    # refetch grabs posts that it's seen before in case there have been updates
    self.expect_requests_get('http://author/permalink', hentry)

    self.mox.ReplayAll()
    discover(self.source, self.activities[0])
    self.assert_syndicated_posts(
      ('http://author/permalink', 'https://fa.ke/post/url1'),
      ('http://author/permalink', 'https://fa.ke/post/url3'),
      ('http://author/permalink', 'https://fa.ke/post/url5'))
    self.assertFalse(refetch(self.source))

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
    self.expect_requests_get('http://author/', """
    <html class="h-feed">
    <a class="h-entry" href="/2014/08/09"></a>
    </html>""")
    self.expect_requests_get('http://author/2014/08/09', """
    <html class="h-entry">
    <a class="u-url" href="/2014/08/09"></a>
    <a class="u-syndication" href="https://fa.ke/post/url"></a>
    </html>""")

    # refetch, permalink has a stub now
    self.expect_requests_get('http://author/', """
    <html class="h-feed">
    <a class="h-entry" href="/2014/08/09/this-is-a-stub"></a>
    </html>""")

    self.expect_requests_get('http://author/2014/08/09/this-is-a-stub', """
    <html class="h-entry">
    <a class="u-url" href="/2014/08/09/this-is-a-stub"></a>
    <a class="u-syndication" href="https://fa.ke/post/url"></a>
    </html>""")

    # refetch again
    self.expect_requests_get('http://author/', """
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
    # modified activity should have /2014/08/09 as an upstreamDuplicate now
    self.assert_discover(['http://author/2014/08/09'])

    # refetch should find the updated original url -> syndication url.
    # it should *not* find the previously discovered relationship.
    first_results = refetch(self.source)
    self.assertEqual(1, len(first_results))
    new_relations = first_results.get('https://fa.ke/post/url')
    self.assertEqual(1, len(new_relations))
    self.assertEqual('https://fa.ke/post/url', new_relations[0].syndication)
    self.assertEqual('http://author/2014/08/09/this-is-a-stub',
                      new_relations[0].original)

    # second refetch should find nothing because nothing has changed
    # since the previous refetch.
    self.assertFalse(refetch(self.source))

  def test_refetch_changed_syndication(self):
    """Update syndication links that have changed since our last fetch."""
    SyndicatedPost(parent=self.source.key,
                   original='http://author/permalink',
                   syndication='https://fa.ke/post/url').put()
    self.expect_requests_get('http://author/', """
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="/permalink"></a>
        <a class="u-syndication" href="http://fa.ke/changed/url"></a>
      </div>
    </html>""")

    self.mox.ReplayAll()
    results = refetch(self.source)
    self.assert_syndicated_posts(
      ('http://author/permalink', 'https://fa.ke/changed/url'))
    self.assert_equals(['https://fa.ke/changed/url'], list(results.keys()))
    self.assert_entities_equal(
      list(SyndicatedPost.query()), results['https://fa.ke/changed/url'])
    self.assertEqual(NOW, self.source.updates['last_syndication_url'])
    self.assertEqual(NOW, self.source.updates['last_feed_syndication_url'])

  def test_refetch_deleted_syndication(self):
    """Deleted syndication links that have disappeared since our last fetch."""
    SyndicatedPost(parent=self.source.key,
                   original='http://author/permalink',
                   syndication='https://fa.ke/post/url').put()
    self.expect_requests_get('http://author/', """
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="/permalink"></a>
      </div>
    </html>""")
    self.expect_requests_get('http://author/permalink', """
      <html class="h-entry">
        <a class="u-url" href="/permalink"></a>
      </html>""")

    self.mox.ReplayAll()
    self.assert_equals({}, refetch(self.source))
    self.assert_syndicated_posts(('http://author/permalink', None))

  def test_refetch_blank_syndication(self):
    """We should preserve blank SyndicatedPosts during refetches."""
    blank = SyndicatedPost(parent=self.source.key,
                           original='http://author/permalink',
                           syndication=None)
    blank.put()
    self.expect_requests_get('http://author/', """
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="/permalink"></a>
      </div>
    </html>""")
    self.expect_requests_get('http://author/permalink', """
      <html class="h-entry">
        <a class="u-url" href="/permalink"></a>
      </html>""")

    self.mox.ReplayAll()
    self.assert_equals({}, refetch(self.source))
    self.assert_syndicated_posts(('http://author/permalink', None))

  def test_refetch_unchanged_syndication(self):
    """We should preserve unchanged SyndicatedPosts during refetches."""
    synd = SyndicatedPost(parent=self.source.key,
                          original='http://author/permalink',
                          syndication='https://fa.ke/post/url')
    synd.put()
    self.expect_requests_get('http://author/', """
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="/permalink"></a>
        <a class="u-syndication" href="https://fa.ke/post/url"></a>
      </div>
    </html>""")

    self.mox.ReplayAll()
    refetch(self.source)
    self.assert_entities_equal([synd], list(SyndicatedPost.query()))

  def test_refetch_with_last_feed_syndication_url_skips_permalinks(self):
    self.source.last_feed_syndication_url = datetime(1970, 1, 1, tzinfo=timezone.utc)
    self.source.put()

    self.expect_requests_get('http://author/', """
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="/permalink"></a>
      </div>
    </html>""")
    # *don't* expect permalink fetch

    self.mox.ReplayAll()
    self.assert_equals({}, refetch(self.source))
    self.assert_syndicated_posts(('http://author/permalink', None))

  def test_refetch_dont_follow_other_silo_syndication(self):
    """We should only resolve redirects if the initial domain is our silo."""
    self.unstub_requests_head()
    self.expect_requests_head('http://author/')
    self.expect_requests_get('http://author/', """
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="/permalink"></a>
        <a class="u-syndication" href="https://oth.er/post/url"></a>
      </div>
    </html>""")
    self.expect_requests_head('http://author/permalink')
    self.expect_requests_get('http://author/permalink')

    self.mox.ReplayAll()
    refetch(self.source)

    synds = list(SyndicatedPost.query())
    self.assertEqual(1, len(synds))
    self.assertEqual('http://author/permalink', synds[0].original)
    self.assertIsNone(synds[0].syndication)

  def test_refetch_syndication_url_head_error(self):
    """We should ignore syndication URLs that 4xx or 5xx."""
    self.expect_requests_head('http://author/')
    self.expect_requests_get('http://author/', """
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="http://author/post"></a>
        <a class="u-syndication" href="https://fa.ke/post/url"></a>
      </div>
    </html>""")
    self.expect_requests_head('http://author/post')
    self.expect_requests_get('http://author/post')
    self.expect_requests_head('https://fa.ke/post/url', status_code=404)

    self.mox.ReplayAll()
    refetch(self.source)

    self.assert_syndicated_posts(('http://author/post', None))

  def test_refetch_synd_url_on_other_silo(self):
    """We should ignore syndication URLs on other (silos') domains."""
    self.expect_requests_get('http://author/', """
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="http://author/post/url"></a>
        <a class="u-syndication" href="http://other/silo/url"></a>
      </div>
    </html>""")
    self.expect_requests_get('http://author/post/url')

    self.mox.ReplayAll()
    refetch(self.source)

    self.assert_syndicated_posts(('http://author/post/url', None))

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
    self.expect_requests_get('http://author/', """
<html class="h-feed">
  <div class="h-entry">
    <a class="u-url h-cite" href="/permalink">this is a strange permalink</a>
  </div>
</html>""")

    self.mox.ReplayAll()
    self.assert_discover([])

  def test_merge_front_page_and_h_feed(self):
    """Make sure we are correctly merging the front page and rel-feed by
    checking that we visit h-entries that are only the front page or
    only the rel-feed page.
    """
    self.expect_requests_get('http://author/', """
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
      self.expect_requests_get(f'http://author{orig}',
                               f"""<div class="h-entry">
                                 <a class="u-url" href="{orig}"></a>
                               </div>""").InAnyOrder()

    self.mox.ReplayAll()
    discover(self.source, self.activity)
    # should be three blank SyndicatedPosts now
    self.assert_syndicated_posts(('http://author/only-on-frontpage', None),
                                 ('http://author/on-both', None),
                                 ('http://author/only-on-feed', None),
                                 (None, 'https://fa.ke/post/url'))

  def test_url_in_activity_not_object(self):
    """We should use the url field in the activity if object doesn't have it.

    setUp() sets self.activity['object']['url'], so the other tests test that case.
    """
    del self.activity['object']['url']
    self.activity['url'] = 'http://www.fa.ke/post/url'

    self.expect_requests_get('http://author/', """
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="http://author/post/url"></a>
        <a class="u-syndication" href="http://www.fa.ke/post/url"></a>
      </div>
    </html>""")

    self.mox.ReplayAll()
    self.assert_discover(['http://author/post/url'])

  def test_skip_non_string_u_urls(self):
    """Make sure that we do not abort due to u-urls that contain objects
    """
    self.expect_requests_get('http://author/', """
    <link rel="feed" href="/feed">
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="http://author/post-with-mistake"></a>
        <a class="u-url h-card" href="http://author/dummy-url">someone made a mistake</a>
      </div>
    </html>""")

    self.expect_requests_get('http://author/feed', """
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url h-card" href="http://author/dummy-url">someone made a mistake</a>
        <a class="u-url" href="http://author/post-with-mistake"></a>
      </div>
      </div>
      <div class="h-entry">
        <a class="u-url" href="http://author/only-on-feed"></a>
      </div>
      <div class="h-entry">
        <a class="u-url h-card" href="http://author/dummy-url">someone made a mistake, and no correct link</a>
      </div>
    </html>""")

    for orig in ('/post-with-mistake', '/only-on-feed'):
      self.expect_requests_get(f'http://author{orig}',
                               f"""<div class="h-entry">
                                 <a class="u-url" href="{orig}"></a>
                               </div>""").InAnyOrder()

    self.mox.ReplayAll()
    discover(self.source, self.activity)
    # should have found both posts successfully
    self.assert_syndicated_posts(('http://author/post-with-mistake', None),
                                 ('http://author/only-on-feed', None),
                                 (None, 'https://fa.ke/post/url'))

  def test_default_strip_fragments(self):
    """We should strip fragments in syndication URLs by default.

    ...even across resolving redirects.
    https://github.com/snarfed/bridgy/issues/984
    """
    self.expect_requests_get('http://author/', """
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="http://author/post"></a>
        <a class="u-syndication" href="http://fa.ke/post#frag"></a>
      </div>
    </html>""")

    self.mox.ReplayAll()
    result = refetch(self.source)
    self.assertCountEqual(['https://fa.ke/post'], result.keys(), result.keys())
    self.assert_syndicated_posts(('http://author/post', 'https://fa.ke/post'))

  def test_drop_reserved_hosts(self):
    """We should should drop URLs with reserved and local hostnames."""
    self.mox.StubOutWithMock(original_post_discovery, 'DEBUG')
    original_post_discovery.DEBUG = False

    self.mox.ReplayAll()
    self.activity['object']['content'] = 'http://localhost http://other/link https://x.test/ http://y.local/path'
    self.assert_discover([], fetch_hfeed=False)

  def test_github_preserve_fragments(self):
    """GitHub sources should preserve fragments in syndication URLs.

    ...even across resolving redirects.
    https://github.com/snarfed/bridgy/issues/984
    """
    self.expect_requests_get('http://author/', """
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="http://author/post"></a>
        <a class="u-syndication" href="https://github.com/post#frag"></a>
      </div>
    </html>""")

    self.mox.ReplayAll()
    self.source = GitHub(id='snarfed', auth_entity=self.auth_entities[0].put(),
                         domain_urls=['http://author/'], domains=['author'])
    self.source.put()

    result = refetch(self.source)
    self.assertCountEqual(['https://github.com/post#frag'], result.keys(),
                          result.keys())

    self.activity['object']['url'] = 'https://github.com/post'
    self.assert_discover(['http://author/post'])
    self.assert_syndicated_posts(('http://author/post', 'https://github.com/post#frag'))
