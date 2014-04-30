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
from requests.exceptions import HTTPError

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

    self.expect_requests_get('http://author', """
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="http://author/post/permalink"></a>
      </div>
    </html>""")

    # syndicated to two places
    self.expect_requests_get('http://author/post/permalink', """
    <link rel="syndication" href="http://not.real/statuses/postid">
    <link rel="syndication" href="http://fa.ke/post/url">
    <div class="h-entry">
      <a class="u-url" href="http://author/post/permalink"></a>
    </div>""")

    self.mox.ReplayAll()
    logging.debug('Original post discovery %s -> %s', source, activity)
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
    every new post. Test that original post discovery does the reverse
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

    self.expect_requests_get('http://author', author_feed)

    # first post is syndicated
    self.expect_requests_get('http://author/post/permalink1', """
    <div class="h-entry">
      <a class="u-url" href="http://author/post/permalink1"></a>
      <a class="u-syndication" href="http://fa.ke/post/url1"></a>
    </div>""").InAnyOrder()

    # second post is syndicated
    self.expect_requests_get('http://author/post/permalink2', """
    <div class="h-entry">
      <a class="u-url" href="http://author/post/permalink2"></a>
      <a class="u-syndication" href="http://fa.ke/post/url2"></a>
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
    activity['object']['content'] = 'with a backlink http://target1/post/url'
    activity['object']['url'] = 'http://fa.ke/post/url'

    original = 'http://target1/post/url'
    syndicated = 'http://fa.ke/post/url'

    self.expect_requests_get('http://target1', """
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="%s"></a>
      </div>
    </html>""" % original)
    self.expect_requests_get(original, """
    <div class="h-entry">
      <a class="u-url" href="%s"></a>
      <a class="u-syndication" href="%s"></a>
    </div>""" % (original, syndicated))

    self.mox.ReplayAll()
    logging.debug('Original post discovery %s -> %s', source, activity)

    wmtargets = tasks.get_webmention_targets(source, activity)
    # activity *will* have a duplicate tag for the original post, one
    # discovered in the post content, one from the rel=syndication
    # lookup.
    self.assertEquals([None, None, original, original],
                      [tag.get('url') for tag in activity['object']['tags']])
    # webmention targets converts to a set to remove duplicates
    self.assertEquals(set([original]), wmtargets)

  def test_rel_feed_link(self):
    """Check that we follow the rel=feed link when looking for the
    author's full feed URL
    """
    source = self.sources[0]
    source.domain_url = 'http://author'
    activity = self.activities[0]

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
    logging.debug('Original post discovery %s -> %s', source, activity)
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

    self.expect_requests_get('http://author', """
    <html class="h-feed">
    <p>under construction</p>
    </html>""")

    self.mox.ReplayAll()
    logging.debug('Original post discovery %s -> %s', source, activity)
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

    self.mox.ReplayAll()

    logging.debug('Original post discovery %s -> %s', source, activity)
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

    self.mox.ReplayAll()

    logging.debug('Original post discovery %s -> %s', source, activity)
    original_post_discovery.discover(source, activity)

    # nothing attempted, but we should have saved a placeholder to prevent us
    # from trying again
    self.assert_equals(
      [(None, 'http://fa.ke/post/url')],
      [(relationship.original, relationship.syndication)
       for relationship in SyndicatedPost.query(ancestor=source.key)])

  def _test_failed_domain_url_fetch(self, raise_exception):
    """Make sure something reasonable happens when the author's domain url
    gives an unexpected response
    """
    source = self.sources[0]
    source.domain_url = 'http://author'
    activity = self.activities[0]
    activity['object']['url'] = 'http://fa.ke/post/url'
    activity['object']['content'] = 'content without links'

    if raise_exception:
      self.expect_requests_get('http://author').AndRaise(HTTPError())
    else:
      self.expect_requests_get('http://author', status_code=404)

    self.mox.ReplayAll()
    original_post_discovery.discover(source, activity)

    # nothing attempted, but we should have saved a placeholder to prevent us
    # from trying again
    self.assert_equals(
      [(None, 'http://fa.ke/post/url')],
      [(relationship.original, relationship.syndication)
       for relationship in SyndicatedPost.query(ancestor=source.key)])

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

  def _test_failed_rel_feed_link_fetch(self, raise_exception):
    """An author page with an invalid rel=feed link. We should recover and
    use any h-entries on the main url as a fallback.
    """
    source = self.sources[0]
    source.domain_url = 'http://author'
    activity = self.activities[0]

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
    logging.debug('Original post discovery %s -> %s', source, activity)
    original_post_discovery.discover(source, activity)

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
    source = self.sources[0]
    source.domain_url = 'http://author'
    activity = self.activities[0]
    activity['object']['url'] = 'http://fa.ke/post/url'
    activity['object']['content'] = 'content without links'

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
    original_post_discovery.discover(source, activity)

    # we should have saved placeholders to prevent us from trying the
    # syndication url or permalink again
    self.assert_equals(
      set([('http://author/nonexistent.html', None), (None, 'http://fa.ke/post/url')]),
      set((relationship.original, relationship.syndication)
          for relationship in SyndicatedPost.query(ancestor=source.key)))

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
    source = self.sources[0]
    source.domain_url = None
    activity = self.activities[0]
    activity['object']['url'] = 'http://fa.ke/post/url'
    activity['object']['content'] = 'content without links'

    self.mox.ReplayAll()
    original_post_discovery.discover(source, activity)

    # nothing attempted, and no SyndicatedPost saved
    self.assertFalse(SyndicatedPost.query(ancestor=source.key).get())

  def test_feed_type_application_xml(self):
    """Confirm that we don't follow rel=feeds explicitly marked as
    application/xml.
    """
    source = self.sources[0]
    source.domain_url = 'http://author'
    activity = self.activities[0]
    activity['object']['url'] = 'http://fa.ke/post/url'
    activity['object']['content'] = 'content without links'

    self.expect_requests_get('http://author', """
    <html>
      <head>
        <link rel="feed" type="application/xml" href="/updates.atom">
      </head>
    </html>
    """)

    self.mox.ReplayAll()
    original_post_discovery.discover(source, activity)

  def test_feed_type_unknown(self):
    """Confirm that we look for an h-feed with type=text/html even when
    the type is not given in <link>, and keep looking until we find one.
    """
    source = self.sources[0]
    source.domain_url = 'http://author'
    activity = self.activities[0]
    activity['object']['url'] = 'http://fa.ke/post/url'
    activity['object']['content'] = 'content without links'

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
    resp = requests.Response()
    resp.status_code = 200
    resp.url = activity['object']['url']
    resp.headers['content-type'] = 'text/html'
    requests.head(activity['object']['url'], allow_redirects=True,
                  timeout=HTTP_TIMEOUT).AndReturn(resp)

    # and for the author url
    resp = requests.Response()
    resp.status_code = 200
    resp.url = source.domain_url
    resp.headers['content-type'] = 'text/html'
    requests.head(source.domain_url, allow_redirects=True,
                  timeout=HTTP_TIMEOUT).AndReturn(resp)


    # try to get the atom feed first
    resp = requests.Response()
    resp.status_code = 200
    resp.headers['content-type'] = 'application/xml'
    resp.url = 'http://author/updates.atom'
    requests.head('http://author/updates.atom', allow_redirects=True,
                  timeout=HTTP_TIMEOUT).AndReturn(resp)

    # keep looking for an html feed
    resp = requests.Response()
    resp.status_code = 200
    resp.headers['content-type'] = 'text/html'
    resp.url = 'http://author/updates.html'
    requests.head('http://author/updates.html', allow_redirects=True,
                  timeout=HTTP_TIMEOUT).AndReturn(resp)

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
    self.expect_requests_get('http://author/permalink', """
    <html class="h-entry">
      <p class="p-name">Title</p>
    </html>""")

    self.mox.ReplayAll()
    original_post_discovery.discover(source, activity)

  #TODO activity with existing responses, make sure they're merged right
