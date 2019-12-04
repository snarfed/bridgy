# coding=utf-8
"""Unit tests for blog_webmention.py.
"""
from __future__ import unicode_literals
from __future__ import absolute_import

from future import standard_library
standard_library.install_aliases()
from builtins import range
import urllib.request, urllib.parse, urllib.error

import appengine_config

from mox3 import mox
from oauth_dropins.webutil.util import json_dumps, json_loads
import requests
from webob import exc

import blog_webmention
import models
from models import BlogWebmention
from . import testutil
import util


class BlogWebmentionTest(testutil.HandlerTest):

  def setUp(self):
    super(BlogWebmentionTest, self).setUp()
    self.source = testutil.FakeSource(id='foo.com',
                                      domains=['x.com', 'foo.com', 'y.com'],
                                      features=['webmention'])
    self.source.put()

    self.mox.StubOutWithMock(testutil.FakeSource, 'create_comment')
    self.mention_html = """\
<article class="h-entry"><p class="e-content">
<span class="p-name">my post</span>
http://foo.com/post/1
</p></article>"""

  def get_response(self, source=None, target=None):
    if source is None:
      source = 'http://bar.com/reply'
    if target is None:
      target = 'http://foo.com/post/1'
    body = ('source=%s&target=%s' % (source, target)).encode('utf-8')
    return blog_webmention.application.get_response(
      '/webmention/fake', method='POST', body=body)

  def assert_error(self, expected_error, status=400, **kwargs):
    resp = self.get_response(**kwargs)
    self.assertEquals(status, resp.status_int)
    self.assertIn(expected_error, json_loads(resp.body)['error'])

  def expect_mention(self):
    self.expect_requests_get('http://bar.com/reply', self.mention_html)
    return testutil.FakeSource.create_comment(
      'http://foo.com/post/1', 'foo.com', 'http://foo.com/',
      'mentioned this in <a href="http://bar.com/reply">my post</a>. <br /> <a href="http://bar.com/reply">via bar.com</a>')

  def test_success(self):
    self._test_success("""
<article class="h-entry">
<p class="p-author">my name</p>
<p class="e-content">
i hereby reply
<a class="u-in-reply-to" href="http://foo.com/post/1"></a>
</p></article>""")

  def test_nested_item_in_hfeed(self):
    """https://chat.indieweb.org/dev/2019-01-23#t1548242942538900"""
    self._test_success("""
<div class="h-feed">
<article class="h-entry">
<p class="p-author">my name</p>
<p class="e-content">
i hereby reply
<a class="u-in-reply-to" href="http://foo.com/post/1"></a>
</p>
</article>
</div>""")

  def _test_success(self, html):
    self.expect_requests_get('http://bar.com/reply', html)

    testutil.FakeSource.create_comment(
      'http://foo.com/post/1', 'my name', 'http://foo.com/',
      'i hereby reply\n<a class="u-in-reply-to" href="http://foo.com/post/1"></a>'
      ' <br /> <a href="http://bar.com/reply">via bar.com</a>'
      ).AndReturn({'id': 'fake id'})
    self.mox.ReplayAll()

    resp = self.get_response()
    self.assertEquals(200, resp.status_int, resp.body)
    self.assertEquals({'id': 'fake id'}, json_loads(resp.body))

    bw = BlogWebmention.get_by_id('http://bar.com/reply http://foo.com/post/1')
    self.assertEquals(self.source.key, bw.source)
    self.assertEquals('complete', bw.status)
    self.assertEquals('comment', bw.type)
    self.assertEquals(html, bw.html)
    self.assertEquals({'id': 'fake id'}, bw.published)

  def test_reply_outside_e_content(self):
    html = """
<article class="h-entry">
<p class="p-author">my name</p>
<p class="p-in-reply-to h-cite"><a href="http://foo.com/post/1"></a></p>
<div class="e-content">
i hereby reply
</div></article>"""
    self.expect_requests_get('http://bar.com/reply', html)

    testutil.FakeSource.create_comment(
      'http://foo.com/post/1', 'my name', 'http://foo.com/',
      'i hereby reply <br /> <a href="http://bar.com/reply">via bar.com</a>'
      ).AndReturn({'id': 'fake id'})
    self.mox.ReplayAll()

    resp = self.get_response()
    self.assertEquals(200, resp.status_int, resp.body)

    bw = BlogWebmention.get_by_id('http://bar.com/reply http://foo.com/post/1')
    self.assertEquals('complete', bw.status)
    self.assertEquals({'id': 'fake id'}, bw.published)
    self.assertEquals(html, bw.html)

  def test_domain_not_found(self):
    self.expect_requests_get('http://foo.com/post/1', status_code=404)
    for i in range(4):
      self.expect_requests_get('http://foo.com/post/1', '')
    self.mox.ReplayAll()

    # couldn't fetch source URL
    self.source.key.delete()
    self.assert_error('Could not fetch source URL http://foo.com/post/1')
    self.assertEquals(0, BlogWebmention.query().count())

    # no source
    msg = 'Could not find FakeSource account for foo.com.'
    self.assert_error(msg)
    self.assertEquals(0, BlogWebmention.query().count())

    # source without webmention feature
    self.source.features = ['listen']
    self.source.put()
    self.assert_error(msg)
    self.assertEquals(0, BlogWebmention.query().count())

    # source without domain
    self.source.features = ['webmention']
    self.source.domains = ['asdfoo.com', 'foo.comedy']
    self.source.put()
    self.assert_error(msg)
    self.assertEquals(0, BlogWebmention.query().count())

    # source is disabled
    self.source.domains = ['foo.com']
    self.source.status = 'disabled'
    self.source.put()
    self.assert_error(msg)
    self.assertEquals(0, BlogWebmention.query().count())

  def test_rel_canonical_different_domain(self):
    self.expect_requests_get('http://foo.zz/post/1', """
<head>
<link href='http://foo.com/post/1' rel='canonical'/>
</head>
foo bar""")

    html = """
<article class="h-entry"><p class="e-content">
<a href="http://bar.com/mention">this post</a>
i hereby <a href="http://foo.zz/post/1">mention</a>
</p></article>"""
    self.expect_requests_get('http://bar.com/mention', html)

    testutil.FakeSource.create_comment(
      'http://foo.zz/post/1', 'foo.zz', 'http://foo.zz/',
      'mentioned this in <a href="http://bar.com/mention">bar.com/mention</a>. <br /> <a href="http://bar.com/mention">via bar.com</a>')
    self.mox.ReplayAll()

    resp = self.get_response('http://bar.com/mention', 'http://foo.zz/post/1')
    self.assertEquals(200, resp.status_int, resp.body)

    bw = BlogWebmention.get_by_id('http://bar.com/mention http://foo.zz/post/1')
    self.assertEquals('complete', bw.status)
    self.assertEquals(html, bw.html)

  def test_target_is_home_page(self):
    self.assert_error('Home page webmentions are not currently supported.',
                      target='http://foo.com/', status=202)
    self.assertEquals(0, BlogWebmention.query().count())

  def test_mention(self):
    html = """\
<article class="h-entry"><p class="e-content">
<span class="p-name">my post</span>
<a href="http://bar.com/mention">this post</a>
<a href="http://foo.com/post/1">another post</a>
</p></article>"""
    self.expect_requests_get('http://bar.com/mention', html)
    testutil.FakeSource.create_comment(
      'http://foo.com/post/1', 'foo.com', 'http://foo.com/',
      'mentioned this in <a href="http://bar.com/mention">my post</a>. <br /> <a href="http://bar.com/mention">via bar.com</a>')
    self.mox.ReplayAll()

    resp = self.get_response('http://bar.com/mention')
    self.assertEquals(200, resp.status_int, resp.body)

  def test_domain_translates_to_lowercase(self):
    html = """\
<article class="h-entry"><p class="e-content">
<span class="p-name">my post</span>
X http://FoO.cOm/post/1
</p></article>"""
    self.expect_requests_get('http://bar.com/reply', html)

    testutil.FakeSource.create_comment(
      'http://FoO.cOm/post/1', 'foo.com', 'http://foo.com/',
      'mentioned this in <a href="http://bar.com/reply">my post</a>. <br /> <a href="http://bar.com/reply">via bar.com</a>')
    self.mox.ReplayAll()

    resp = self.get_response(target='http://FoO.cOm/post/1')
    self.assertEquals(200, resp.status_int, resp.body)
    bw = BlogWebmention.get_by_id('http://bar.com/reply http://FoO.cOm/post/1')
    self.assertEquals('complete', bw.status)

  def test_source_link_not_found(self):
    html = '<article class="h-entry"></article>'
    self.expect_requests_get('http://bar.com/reply', html)
    self.mox.ReplayAll()
    self.assert_error('Could not find target URL')
    bw = BlogWebmention.get_by_id('http://bar.com/reply http://foo.com/post/1')
    self.assertEquals('failed', bw.status)
    self.assertEquals(html, bw.html)

  def test_target_path_blacklisted(self):
    bad = 'http://foo.com/blacklisted/1'
    self.assert_error(
      'FakeSource webmentions are not supported for URL path: /blacklisted/1',
      target=bad, status=202)
    self.assertEquals(0, BlogWebmention.query().count())

  def test_strip_utm_query_params(self):
    """utm_* query params should be stripped from target URLs."""
    self.expect_mention()
    self.mox.ReplayAll()

    resp = self.get_response(target=urllib.parse.quote(
        'http://foo.com/post/1?utm_source=x&utm_medium=y'))
    self.assertEquals(200, resp.status_int, resp.body)
    bw = BlogWebmention.get_by_id('http://bar.com/reply http://foo.com/post/1')
    self.assertEquals('complete', bw.status)

  def test_unicode_in_target_and_source_urls(self):
    """Unicode chars in target and source URLs should work."""
    # note the … and ✁ chars
    target = 'http://foo.com/2014/11/23/england-german…iendly-wembley'
    source = 'http://bar.com/✁/1'

    html = u"""\
<meta charset="utf-8">
<article class="h-entry"><p class="e-content">
<span class="p-name">my post</span>
%s
</p></article>""" % target
    self.expect_requests_get(source, html)

    comment = 'mentioned this in <a href="%s">my post</a>. <br /> <a href="%s">via bar.com</a>' % (source, source)
    testutil.FakeSource.create_comment(target, 'foo.com', 'http://foo.com/', comment)
    self.mox.ReplayAll()

    resp = self.get_response(source=source, target=target)
    self.assertEquals(200, resp.status_int, resp.body)
    bw = BlogWebmention.get_by_id(' '.join((source, target)))
    self.assertEquals('complete', bw.status)

  def test_target_redirects(self):
    html = """\
<article class="h-entry"><p class="e-content">
http://second/
</p></article>"""
    redirects = ['http://second/', 'http://foo.com/final']
    self.expect_requests_head('http://first/', redirected_url=redirects)
    self.expect_requests_get('http://bar.com/reply', html)
    testutil.FakeSource.create_comment(
      'http://foo.com/final', 'foo.com', 'http://foo.com/', mox.IgnoreArg())
    self.mox.ReplayAll()

    resp = self.get_response(target='http://first/')
    self.assertEquals(200, resp.status_int, resp.body)
    bw = BlogWebmention.get_by_id('http://bar.com/reply http://foo.com/final')
    self.assertEquals('complete', bw.status)
    self.assertEquals(['http://first/', 'http://second/'], bw.redirected_target_urls)

  def test_source_link_check_ignores_fragment(self):
    html = """\
<article class="h-entry"><p class="e-content">
<a href="http://bar.com/reply">(permalink)</a>
<span class="p-name">my post</span>
<a href="http://foo.com/post/1"></a>
</p></article>"""
    self.expect_requests_get('http://bar.com/reply', html)
    testutil.FakeSource.create_comment(
      'http://foo.com/post/1', 'foo.com', 'http://foo.com/',
      'mentioned this in <a href="http://bar.com/reply">my post</a>. <br /> <a href="http://bar.com/reply">via bar.com</a>')
    self.mox.ReplayAll()

    resp = self.get_response()
    self.assertEquals(200, resp.status_int, resp.body)
    bw = BlogWebmention.get_by_id('http://bar.com/reply http://foo.com/post/1')
    self.assertEquals('complete', bw.status)

  def test_source_missing_mf2(self):
    html = 'no microformats here, run along'
    self.expect_requests_get('http://bar.com/reply', html)
    self.mox.ReplayAll()
    self.assert_error('No microformats2 data found')
    bw = BlogWebmention.get_by_id('http://bar.com/reply http://foo.com/post/1')
    self.assertEquals('failed', bw.status)
    self.assertEquals(html, bw.html)

  def test_u_url(self):
    html = """
<article class="h-entry">
<p class="p-name"></p> <!-- empty -->
<p class="p-author">my name</p>
<p class="e-content">
i hereby mention
<a href="http://foo.com/post/1"></a>
<a class="u-url" href="http://barzz.com/u/url"></a>
</p></article>"""
    self.expect_requests_get('http://bar.com/reply', html)

    testutil.FakeSource.create_comment(
      'http://foo.com/post/1', 'my name', 'http://foo.com/', """mentioned this in <a href="http://barzz.com/u/url">barzz.com/u/url</a>. <br /> <a href="http://barzz.com/u/url">via barzz.com</a>"""
      ).AndReturn({'id': 'fake id'})
    self.mox.ReplayAll()

    resp = self.get_response()
    self.assertEquals(200, resp.status_int, resp.body)
    bw = BlogWebmention.get_by_id('http://bar.com/reply http://foo.com/post/1')
    self.assertEquals('complete', bw.status)
    self.assertEquals('post', bw.type)
    self.assertEquals('http://barzz.com/u/url', bw.u_url)
    self.assertEquals('http://barzz.com/u/url', bw.source_url())

  def test_repeated(self):
    # 1) first a failure
    self.expect_requests_get('http://bar.com/reply', '')

    # 2) should allow retrying, this one will succeed
    self.expect_requests_get('http://bar.com/reply', """
<article class="h-entry">
<a class="u-url" href="http://bar.com/reply"></a>
<a class="u-repost-of" href="http://foo.com/post/1"></a>
</article>""")
    testutil.FakeSource.create_comment(
      'http://foo.com/post/1', 'foo.com', 'http://foo.com/',
      'reposted this. <br /> <a href="http://bar.com/reply">via bar.com</a>')

    # 3) after success, another is a noop and returns 200
    # TODO: check for "updates not supported" message
    self.mox.ReplayAll()

    # now the webmention requests. 1) failure
    self.assert_error('No microformats2 data found')
    bw = BlogWebmention.get_by_id('http://bar.com/reply http://foo.com/post/1')
    self.assertEquals('failed', bw.status)

    # 2) success
    resp = self.get_response()
    self.assertEquals(200, resp.status_int, resp.body)
    bw = BlogWebmention.get_by_id('http://bar.com/reply http://foo.com/post/1')
    self.assertEquals('complete', bw.status)
    self.assertEquals('repost', bw.type)

    # 3) noop repeated success
    # source without webmention feature
    resp = self.get_response()
    self.assertEquals(200, resp.status_int, resp.body)
    bw = BlogWebmention.get_by_id('http://bar.com/reply http://foo.com/post/1')
    self.assertEquals('complete', bw.status)

  def test_create_comment_exception(self):
    self.expect_mention().AndRaise(exc.HTTPPaymentRequired())
    self.mox.ReplayAll()

    resp = self.get_response()
    self.assertEquals(402, resp.status_int, resp.body)
    bw = BlogWebmention.get_by_id('http://bar.com/reply http://foo.com/post/1')
    self.assertEquals('failed', bw.status)
    self.assertEquals(self.mention_html, bw.html)

  def test_create_comment_401_disables_source(self):
    self.expect_mention().AndRaise(exc.HTTPUnauthorized('no way'))
    self.mox.ReplayAll()

    self.assert_error('no way', status=401)
    source = self.source.key.get()
    self.assertEquals('disabled', source.status)

    bw = BlogWebmention.get_by_id('http://bar.com/reply http://foo.com/post/1')
    self.assertEquals('failed', bw.status)
    self.assertEquals(self.mention_html, bw.html)

  def test_create_comment_404s(self):
    self.expect_mention().AndRaise(exc.HTTPNotFound('gone baby gone'))
    self.mox.ReplayAll()

    self.assert_error('gone baby gone', status=404)

    bw = BlogWebmention.get_by_id('http://bar.com/reply http://foo.com/post/1')
    self.assertEquals('failed', bw.status)
    self.assertEquals(self.mention_html, bw.html)

  def test_create_comment_500s(self):
    self.expect_mention().AndRaise(exc.HTTPInternalServerError('oops'))
    self.mox.ReplayAll()
    self.assert_error('oops', status=util.ERROR_HTTP_RETURN_CODE)

  def test_create_comment_raises_connection_error(self):
    self.expect_mention().AndRaise(requests.ConnectionError('oops'))
    self.mox.ReplayAll()
    self.assert_error('oops', status=util.ERROR_HTTP_RETURN_CODE)

  def test_sources_global(self):
    self.assertIsNotNone(models.sources['blogger'])
    self.assertIsNotNone(models.sources['tumblr'])
    self.assertIsNotNone(models.sources['wordpress'])
