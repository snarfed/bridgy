"""Unit tests for blog_webmention.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import json
import mox
import urllib
from webob import exc

import appengine_config
from models import BlogWebmention
import blog_webmention
import testutil


class BlogWebmentionTest(testutil.HandlerTest):

  def setUp(self):
    super(BlogWebmentionTest, self).setUp()
    blog_webmention.SOURCES['fake'] = testutil.FakeSource
    self.source = testutil.FakeSource(id='foo.com',
                                      domains=['x.com', 'foo.com', 'y.com'],
                                      features=['webmention'])
    self.source.put()

    self.mox.StubOutWithMock(testutil.FakeSource, 'create_comment')

  def get_response(self, source=None, target=None):
    if source is None:
      source = 'http://bar.com/reply'
    if target is None:
      target = 'http://foo.com/post/1'
    return blog_webmention.application.get_response(
      '/webmention/fake', method='POST',
      body='source=%s&target=%s' % (source, target))

  def assert_error(self, expected_error, status=400, **kwargs):
    resp = self.get_response(**kwargs)
    self.assertEquals(status, resp.status_int)
    self.assertIn(expected_error, json.loads(resp.body)['error'])

  def test_success(self):
    html = """
<article class="h-entry">
<p class="p-author">my name</p>
<p class="e-content">
i hereby reply
<a class="u-in-reply-to" href="http://foo.com/post/1"></a>
</p></article>"""
    self.expect_requests_get('http://bar.com/reply', html)

    testutil.FakeSource.create_comment(
      'http://foo.com/post/1', 'my name', 'http://foo.com/',
      'i hereby reply\n<a class="u-in-reply-to" href="http://foo.com/post/1"></a>'
      ' <br /> <a href="http://bar.com/reply">via bar.com</a>'
      ).AndReturn({'id': 'fake id'})
    self.mox.ReplayAll()

    resp = self.get_response()
    self.assertEquals(200, resp.status_int, resp.body)
    self.assertEquals({'id': 'fake id'}, json.loads(resp.body))

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
    # no source
    msg = 'Could not find FakeSource account for foo.com.'
    self.source.key.delete()
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

  def test_target_is_home_page(self):
    self.assert_error('Home page webmentions are not currently supported.',
                      target='http://foo.com/')
    self.assertEquals(0, BlogWebmention.query().count())

  def test_mention(self):
    html = """\
<article class="h-entry"><p class="e-content">
<span class="p-name">my post</span>
foo
<a href="http://foo.com/post/1">this post</a>
</p></article>"""
    self.expect_requests_get('http://bar.com/reply', html)
    testutil.FakeSource.create_comment(
      'http://foo.com/post/1', 'foo.com', 'http://foo.com/',
      'mentioned this in <a href="http://bar.com/reply">my post</a>. <br /> <a href="http://bar.com/reply">via bar.com</a>')
    self.mox.ReplayAll()

    resp = self.get_response()
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

  def test_strip_utm_query_params(self):
    """utm_* query params should be stripped from target URLs."""
    html = """\
<article class="h-entry"><p class="e-content">
<span class="p-name">my post</span>
http://foo.com/post/1
</p></article>"""
    self.expect_requests_get('http://bar.com/reply', html)
    testutil.FakeSource.create_comment(
      'http://foo.com/post/1', 'foo.com', 'http://foo.com/',
      'mentioned this in <a href="http://bar.com/reply">my post</a>. <br /> <a href="http://bar.com/reply">via bar.com</a>')
    self.mox.ReplayAll()

    resp = self.get_response(target=urllib.quote(
        'http://foo.com/post/1?utm_source=x&utm_medium=y'))
    self.assertEquals(200, resp.status_int, resp.body)
    bw = BlogWebmention.get_by_id('http://bar.com/reply http://foo.com/post/1')
    self.assertEquals('complete', bw.status)

  def test_source_link_check_ignores_fragment(self):
    html = """\
<article class="h-entry"><p class="e-content">
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
    html = """\
<article class="h-entry"><p class="e-content">
<span class="p-name">my post</span>
http://foo.com/post/1
</p></article>"""
    self.expect_requests_get('http://bar.com/reply', html)
    testutil.FakeSource.create_comment(
      'http://foo.com/post/1', 'foo.com', 'http://foo.com/', mox.IgnoreArg()
      ).AndRaise(exc.HTTPPaymentRequired())
    self.mox.ReplayAll()

    resp = self.get_response()
    self.assertEquals(402, resp.status_int, resp.body)
    bw = BlogWebmention.get_by_id('http://bar.com/reply http://foo.com/post/1')
    self.assertEquals('failed', bw.status)
    self.assertEquals(html, bw.html)
