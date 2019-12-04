# coding=utf-8
"""Unit tests for publish.py.
"""
from __future__ import unicode_literals
from __future__ import absolute_import

from future.utils import native_str
from future import standard_library
standard_library.install_aliases()
from builtins import range
import socket
import urllib.request, urllib.parse, urllib.error

import appengine_config

from granary import source as gr_source
from mox3 import mox
from oauth_dropins.webutil.testutil import requests_response
from oauth_dropins.webutil.util import json_dumps, json_loads
import requests
import webapp2
from webob import exc

import facebook
from models import Publish, PublishedPage
import publish
from . import testutil
import util


class PublishTest(testutil.HandlerTest):

  def setUp(self):
    super(PublishTest, self).setUp()
    publish.SOURCE_NAMES['fake'] = testutil.FakeSource
    publish.SOURCE_DOMAINS['fa.ke'] = testutil.FakeSource

    self.auth_entity = testutil.FakeAuthEntity(id='0123456789')
    self.source = testutil.FakeSource(
      id='foo.com', features=['publish'], domains=['foo.com'],
      domain_urls=['http://foo.com/'], auth_entity=self.auth_entity.key)
    self.source.put()

    self.oauth_state = {
      'source_url': 'http://foo.com/bar',
      'target_url': 'https://brid.gy/publish/fake',
      'source_key': self.source.key.urlsafe(),
      'include_link': gr_source.INCLUDE_LINK,
    }
    self.post_html = '<article class="h-entry"><p class="e-content">%s</p></article>'
    self.backlink = '\n<a href="http://localhost/publish/fake"></a>'

  def get_response(self, source=None, target=None, preview=False,
                   interactive=False, params=None):
    if params is None:
      params = {}
    params.update({
      'source': source or 'http://foo.com/bar',
      'target': target or 'https://brid.gy/publish/fake',
      'source_key': self.source.key.urlsafe(),
      })

    app = publish.application
    assert not (preview and interactive)
    if interactive:
      class FakeSendHandler(publish.SendHandler):
        def post(fsh_self):
          state = (util.encode_oauth_state(self.oauth_state)
                   if self.oauth_state else None)
          fsh_self.finish(self.auth_entity, state)
      app = webapp2.WSGIApplication([('.*', FakeSendHandler)])

    return app.get_response(
      '/publish/preview' if preview else '/publish/webmention',
      method='POST', body=native_str(urllib.parse.urlencode(params)))

  def expect_requests_get(self, url, body='', backlink=None, **kwargs):
    body += backlink or self.backlink
    resp = super(PublishTest, self).expect_requests_get(url, body, **kwargs)
    return resp

  def assert_response(self, expected, status=None, preview=False, **kwargs):
    resp = self.get_response(preview=preview, **kwargs)
    body = resp.body.decode('utf-8')
    self.assertEquals(status, resp.status_int,
                      '%s != %s: %s' % (status, resp.status_int, body))
    if preview:
      self.assertIn(expected, body,
                    '%r\n\n=== vs ===\n\n%r' % (expected, body))
    else:
      if resp.headers['Content-Type'] == 'application/json':
        body = json_loads(body)['content' if status < 300 else 'error']
      self.assertIn(expected, body)

    return resp

  def assert_success(self, expected, **kwargs):
    return self.assert_response(expected, status=200, **kwargs)

  def assert_created(self, expected, **kwargs):
    return self.assert_response(expected, status=201, **kwargs)

  def assert_error(self, expected, status=400, **kwargs):
    return self.assert_response(expected, status=status, **kwargs)

  def _check_entity(self, content='foo', html_content=None):
    if html_content is None:
      html_content = content
    self.assertTrue(PublishedPage.get_by_id('http://foo.com/bar'))
    publish = Publish.query().get()
    self.assertEquals(self.source.key, publish.source)
    self.assertEquals('complete', publish.status)
    self.assertEquals('post', publish.type)
    self.assertEquals('FakeSource post label', publish.type_label())
    expected_html = (self.post_html % html_content) + self.backlink
    self.assertEquals(expected_html, publish.html)
    self.assertEquals({
      'id': 'fake id',
      'url': 'http://fake/url',
      'content': '%s - http://foo.com/bar' % content,
      'granary_message': 'granary message',
    }, publish.published)

  def test_webmention_success(self):
    self.expect_requests_get('http://foo.com/bar', self.post_html % 'foo')
    self.mox.ReplayAll()
    resp = self.assert_created('foo - http://foo.com/bar', interactive=False)
    self.assertEquals('http://fake/url', resp.headers['Location'])
    self._check_entity()

  def test_interactive_success(self):
    self.expect_requests_get('http://foo.com/bar', self.post_html % 'foo')
    self.mox.ReplayAll()

    resp = self.get_response(interactive=True)
    self.assertEquals(302, resp.status_int)
    self.assertEquals(
      'http://localhost/fake/foo.com#!'
        'Done! <a href="http://fake/url">Click here to view.</a>\ngranary message',
      urllib.parse.unquote_plus(resp.headers['Location']))
    self._check_entity()

  def test_interactive_from_wrong_user_page(self):
    other_source = testutil.FakeSource.new(None).put()
    self.oauth_state['source_key'] = other_source.urlsafe()

    resp = self.get_response(interactive=True)
    self.assertEquals(302, resp.status_int)
    self.assertEquals(
      'http://localhost/fake/%s#!'
        'Please log into FakeSource as fake to publish that page.' %
        other_source.id(),
      urllib.parse.unquote_plus(resp.headers['Location']))

    self.assertIsNone(Publish.query().get())

  def test_interactive_oauth_decline(self):
    self.auth_entity = None
    resp = self.get_response(interactive=True)
    self.assertEquals(302, resp.status_int)
    self.assertEquals(
      'http://localhost/fake/foo.com#!'
        'If you want to publish or preview, please approve the prompt.',
      urllib.parse.unquote_plus(resp.headers['Location']))

    self.assertIsNone(Publish.query().get())

  def test_interactive_no_state(self):
    """https://github.com/snarfed/bridgy/issues/449"""
    self.oauth_state = None
    resp = self.get_response(interactive=True)
    self.assertEquals(302, resp.status_int)
    self.assertEquals(
      'http://localhost/#!'
        'If you want to publish or preview, please approve the prompt.',
      urllib.parse.unquote_plus(resp.headers['Location']))

    self.assertIsNone(Publish.query().get())

  def test_success_domain_translates_to_lowercase(self):
    self.expect_requests_get('http://FoO.cOm/Bar', self.post_html % 'foo')
    self.mox.ReplayAll()
    self.assert_created('foo - http://FoO.cOm/Bar', source='http://FoO.cOm/Bar')

  def test_success_domain_http_vs_https(self):
    self.expect_requests_get('https://foo.com/bar', self.post_html % 'foo')
    self.mox.ReplayAll()
    self.assert_created('foo - https://foo.com/bar', source='https://foo.com/bar')

  def test_success_source_status_error(self):
    """Sources in status 'error' should still be able to publish."""
    self.source.status = 'error'
    self.source.put()

    self.expect_requests_get('http://foo.com/bar', self.post_html % 'foo')
    self.mox.ReplayAll()
    self.assert_created('foo - http://foo.com/bar')

  def test_already_published(self):
    """We shouldn't allow duplicating an existing, *completed* publish."""
    page = PublishedPage(id='http://foo.com/bar')

    # these are all fine
    Publish(parent=page.key, source=self.source.key, status='new').put()
    Publish(parent=page.key, source=self.source.key, status='failed').put()
    Publish(parent=page.key, source=self.source.key, status='complete',
            type='preview', published={'content': 'foo'}).put()

    for i in range(5):
      self.expect_requests_get('http://foo.com/bar', self.post_html % 'foo')
    self.mox.ReplayAll()

    # first attempt should work
    self.assert_success('preview of foo - http://foo.com/bar', preview=True)
    created = self.assert_created('foo - http://foo.com/bar')
    self.assertEquals(5, Publish.query().count())
    self.assertEquals(3, Publish.query(Publish.status == 'complete').count())

    # now that there's a complete Publish entity, more attempts should fail
    resp = self.assert_error("Sorry, you've already published that page")
    self.assertEquals(json_loads(created.body), json_loads(resp.body)['original'])

    # try again to test for a bug we had where a second try would succeed
    self.assert_error("Sorry, you've already published that page")
    # should still be able to preview though
    self.assert_success('preview of foo', preview=True)

  def test_already_published_interactive(self):
    self.expect_requests_get('http://foo.com/bar', self.post_html % 'foo')
    self.mox.ReplayAll()

    page = PublishedPage(id='http://foo.com/bar')
    Publish(parent=page.key, source=self.source.key, status='complete',
            type='post', published={'content': 'foo'}).put()

    resp = self.assert_response('', status=302, interactive=True)
    self.assertIn("Sorry, you've already published that page",
                  urllib.parse.unquote_plus(resp.headers['Location']))

  def test_already_published_then_preview_feed_with_no_items(self):
    page = PublishedPage(id='http://foo.com/bar')
    Publish(parent=page.key, source=self.source.key, status='complete',
            type='post', published={'content': 'foo'}).put()

    self.expect_requests_get('http://foo.com/bar', '<div class="h-feed"></div>')
    self.mox.ReplayAll()
    self.assert_success('', preview=True)

  def test_more_than_one_silo(self):
    """POSSE to more than one silo should not trip the already published check"""
    class FauxSource(testutil.FakeSource):
      SHORT_NAME = 'faux'

    publish.SOURCE_NAMES['faux'] = FauxSource
    FauxSource(
      id='foo.com', features=['publish'], domains=['foo.com'],
      domain_urls=['http://foo.com/']).put()

    html = self.post_html % 'foo'
    self.expect_requests_get('http://foo.com/bar', html)
    self.expect_requests_get('http://foo.com/bar', html,
                             backlink='\n<a href="http://localhost/publish/faux"></a>')

    self.mox.ReplayAll()

    self.assert_created('')
    self.assert_created('', target='https://brid.gy/publish/faux')

  def test_bad_target_url(self):
    for target in (
        'foo',
        'https://brid.gy/publish/googleplus',
        'https://brid.gy/publish/instagram',
    ):
      self.assert_error(
        'Target must be brid.gy/publish/{flickr,github,mastodon,twitter}',
        target=target)

  def test_source_url_redirects(self):
    self.expect_requests_head('http://will/redirect', redirected_url='http://foo.com/1')

    self.expect_requests_get('http://foo.com/1', self.post_html % 'foo')
    self.mox.ReplayAll()
    # check that we include the original link, not the resolved one
    self.assert_created('foo - http://will/redirect', source='http://will/redirect')

  def test_source_url_redirects_with_refresh_header(self):
    self.expect_requests_head('http://will/redirect',
                              response_headers={'refresh': '0; url=http://foo.com/1'})
    self.expect_requests_head('http://foo.com/1')

    self.expect_requests_get('http://foo.com/1', self.post_html % 'foo')
    self.mox.ReplayAll()
    # check that we include the original link, not the resolved one
    self.assert_created('foo - http://will/redirect', source='http://will/redirect')

  def test_link_rel_shortlink(self):
    self._test_shortlink("""\
<html>
<head><link rel="shortlink" href="http://foo.com/short" /></head>
<body>
""" + self.post_html % 'foo' + """\
</body>
</html>""")

  def test_expand_link_rel_shortlink(self):
    self._test_shortlink("""\
<html>
<head><link rel="shortlink" href="/short" /></head>
<body>
""" + self.post_html % 'foo' + """\
</body>
</html>""")

  def test_a_rel_shortlink(self):
    self._test_shortlink(self.post_html % """\
foo
<a rel="shortlink" href="http://foo.com/short"></a>""")

  def _test_shortlink(self, html):
    self.expect_requests_get('http://foo.com/bar', html)
    self.mox.ReplayAll()
    self.assert_created('foo - http://foo.com/short')

  def test_rel_shortlink_overrides_redirect(self):
    self.expect_requests_head('http://will/redirect', redirected_url='http://foo.com/1')
    self.expect_requests_get('http://foo.com/1', self.post_html % """\
foo
<a rel="shortlink" href="http://foo.com/short"></a>""")
    self.mox.ReplayAll()
    self.assert_created('foo - http://foo.com/short', source='http://will/redirect')

  def test_bad_source(self):
    # no source
    self.source.key.delete()
    self.assert_error('Could not find <b>FakeSource</b> account for <b>foo.com</b>.')

    # source without publish feature
    self.source.features = ['listen']
    self.source.put()
    msg = 'Publish is not enabled'
    self.assert_error(msg)

    # status disabled
    self.source.features = ['publish']
    self.source.status = 'disabled'
    self.source.put()
    self.assert_error(msg)

    # two bad sources with same domain
    source_2 = self.source = testutil.FakeSource(id='z', **self.source.to_dict())
    source_2.status = 'enabled'
    source_2.features = ['listen']
    source_2.put()
    self.assert_error(msg)

    # one bad source, one good source, same domain. should automatically use the
    # good source.
    source_2.features.append('publish')
    source_2.put()
    self.expect_requests_get('http://foo.com/bar', self.post_html % 'xyz')
    self.mox.ReplayAll()
    self.assert_created('xyz - http://foo.com/bar')
    self.assertEquals(source_2.key, Publish.query().get().source)

  def test_source_with_multiple_domains(self):
    """Publish domain is second in source's domains list."""
    self.source.domains = ['baj.com', 'foo.com']
    self.source.domain_urls = ['http://baj.com/', 'http://foo.com/']
    self.source.put()
    self.expect_requests_get('http://foo.com/bar', self.post_html % 'xyz')
    self.mox.ReplayAll()
    self.assert_created('xyz - http://foo.com/bar')
    self.assertEquals(self.source.key, Publish.query().get().source)

  def test_source_missing_mf2(self):
    self.expect_requests_get('http://foo.com/bar', '')
    self.mox.ReplayAll()
    self.assert_error('No microformats2 data found in http://foo.com/')

    self.assertTrue(PublishedPage.get_by_id('http://foo.com/bar'))
    publish = Publish.query().get()
    self.assertEquals('failed', publish.status)
    self.assertEquals(self.source.key, publish.source)

  def test_h_feed_no_items(self):
    self.expect_requests_get('http://foo.com/bar', '<div class="h-feed"></div>')
    self.mox.ReplayAll()
    self.assert_error('Could not find content')
    self.assertEquals('failed', Publish.query().get().status)

  def test_no_content(self):
    self.expect_requests_get('http://foo.com/bar',
                             '<article class="h-entry"></article>')
    self.mox.ReplayAll()

    self.assert_error('Could not find content')
    self.assertEquals('failed', Publish.query().get().status)

  def test_no_content_ignore_formatting(self):
    self.expect_requests_get('http://foo.com/bar',
                             '<article class="h-entry"></article>')
    self.mox.ReplayAll()

    self.assert_error('Could not find content',
                      params={'bridgy_ignore_formatting': ''})
    self.assertEquals('failed', Publish.query().get().status)

  def test_multiple_items_chooses_first_that_works(self):
    html = ('<a class="h-card" href="http://mic.lim.com/">Mic Lim</a>\n' +
            self.post_html % 'foo')
    self.expect_requests_get('http://foo.com/bar', html)
    self.mox.ReplayAll()
    self.assert_created('foo - http://foo.com/bar')

  def test_unpublishable_type(self):
    html = ('<p class="h-breadcrumb"><span class="e-content">not publishable</span></p>\n' +
            self.post_html % 'foo')
    self.expect_requests_get('http://foo.com/bar', html)
    self.mox.ReplayAll()
    self.assert_created('foo - http://foo.com/bar')

  def test_type_not_implemented(self):
    self.expect_requests_get('http://foo.com/bar', """
<article class="h-entry"><a class="u-like-of" href="xyz">W</a></article>""")
    self.expect_requests_get('http://foo.com/xyz', '')
    self.mox.ReplayAll()

    # FakeSource.create() raises NotImplementedError on likes
    self.assert_error('Cannot publish likes')
    self.assertEquals('failed', Publish.query().get().status)

  def test_source_url_is_domain_url(self):
    self.source.put()
    self.assert_error("Looks like that's your home page.", source='http://foo.com#')

    # query params alone shouldn't trigger this
    self.expect_requests_get('http://foo.com/?p=123', self.post_html % 'foo')
    self.mox.ReplayAll()
    self.assert_created('foo - http://foo.com/?p=123',
                        source='http://foo.com/?p=123')

  def test_source_url_redirects_to_domain_url(self):
    self.expect_requests_head('http://will/redirect', redirected_url='http://foo.com')
    self.mox.ReplayAll()
    self.source.put()
    self.assert_error("Looks like that's your home page.",
                      source='http://will/redirect')

  def test_source_url_is_silo(self):
    self.source.put()
    self.assert_error(
      "Looks like that's a FakeSource URL. Try one from your web site instead!",
      source='http://fa.ke/post/123')
    self.assert_error(
      "Looks like that's a Twitter URL. Try one from your web site instead!",
      source='http://twitter.com/post/123')

  def test_embedded_type_not_implemented(self):
    self.expect_requests_get('http://foo.com/bar', """
<article class="h-entry">
  <div class="p-like-of">
    foo <a class="u-url" href="http://url">bar</a>
  </div>
</article>""")
    self.mox.ReplayAll()

    # FakeSource.create() returns an error message for verb='like'
    self.assert_error("Cannot publish likes")
    self.assertEquals('failed', Publish.query().get().status)

  def test_mf1_backward_compatibility_inside_hfeed(self):
    """This is based on Blogger's default markup, e.g.
    http://daisystanton.blogspot.com/2014/06/so-elections.html
    """
    self.expect_requests_get('http://foo.com/bar', """
<div class="blog-posts hfeed">
<div class="post hentry uncustomized-post-template">
<div class="post-body entry-content">
this is my article
</div></div></div>""")
    self.mox.ReplayAll()
    self.assert_created('this is my article - http://foo.com/bar')

  def test_ignore_hfeed_contents(self):
    """Background in https://github.com/snarfed/bridgy/issues/219"""
    self.expect_requests_get('http://foo.com/bar', """
<div class="blog-posts hfeed">
<div class="e-content">my feed</div>
<div class="h-entry">
<div class="e-content">my article</div>
</div>""")
    self.mox.ReplayAll()
    self.assert_created('my article - http://foo.com/bar')

  def test_tumblr_markup(self):
    """This is based on Tumblr's default markup, e.g.
    http://snarfed.tumblr.com/post/84623272717/stray-cat
    """
    self.expect_requests_get('http://foo.com/bar', """
<body>
<div id="content">
  <div class="post">
    <div class="copy"><p>this is my article</p></div>
    <div class="footer for_permalink"></div>
  </div>
</div>
</body>
""")
    self.mox.ReplayAll()
    self.assert_created('this is my article - http://foo.com/bar')

  def test_tumblr_markup_with_photo(self):
    """A tumblr post with a picture but no text.
    Based on http://require.aorcsik.com/post/98159554316/whitenoisegirl-the-clayprofessor-chris """
    self.expect_requests_get('http://foo.com/bar', """
<body>
<section id="content">
  <section class="post">
    <figure>
      <div class="photo-wrapper">
        <div class="photo-wrapper-inner">
          <a href="http://my/photo/download">
            <img src="http://my/photo/url">
          </a>
        </div>
      </div>
    </figure>
  </section>
</section>
</body>
""")
    self.mox.ReplayAll()
    self.assert_error('Could not find content')

  def test_tumblr_special_case_does_not_override_mf1(self):
    """Tumblr's special case should not add "h-entry" on a class
    that already has mf1 microformats on it (or it will cause the parser
    to ignore the mf2 properties).
    """
    self.expect_requests_get('http://foo.com/bar', """
<!DOCTYPE html>
<html>
<head></head>
<body>
  <div id="content">
    <div class="post hentry">
      <div class="entry-content">blah</div>
      <img class="photo" src="http://baz.org/img.jpg"/>
      <a rel="bookmark" href="http://foo.com/bar"></a>
    </div>
  </div>
</body>
</html>
""")
    self.mox.ReplayAll()
    self.assert_created('blah - http://foo.com/bar')

  def test_tumblr_backlink_in_t_umblr_com_url(self):
    """Tumblr now rewrites links in t.umblr.com wrapper. Handle that.

    https://github.com/snarfed/bridgy/issues/609"""
    link = '<a href="http://t.umblr.com/redirect?z=http%3A%2F%2Flocalhost%2Fpublish%2Ffake&amp;t=YmZkMzQyODJmYjQ5ZmEzNDNlMWI5YmZhYmQ2MWI4NDcyNDNlMjNhOCxCOE9JaXhYUQ%3D%3D"></a>'
    self.expect_requests_get('http://foo.com/bar', self.post_html % 'foo',
                             backlink=link)
    self.mox.ReplayAll()
    self.assert_created('foo - http://foo.com/bar', interactive=False)

  def test_returned_type_overrides(self):
    # FakeSource returns type 'post' when it sees 'rsvp'
    self.expect_requests_get('http://foo.com/bar', """
<article class="h-entry">
<p class="e-content">
<data class="p-rsvp" value="yes"></data>
<a class="u-in-reply-to" href="http://fa.ke/event"></a>
</p></article>""")
    self.mox.ReplayAll()
    self.assert_created('')
    self.assertEquals('post', Publish.query().get().type)

  def test_in_reply_to_domain_allows_subdomains(self):
    """(The code that handles this is in granary.Source.base_object.)"""
    subdomains = 'www.', 'mobile.', ''
    for i, subdomain in enumerate(subdomains):
      self.expect_requests_get('http://foo.com/%d' % i,
"""<div class="h-entry"><p class="e-content">
<a class="u-in-reply-to" href="http://%sfa.ke/a/b/d">foo</a>
</p></div>""" % subdomain)
    self.mox.ReplayAll()

    for i in range(len(subdomains)):
      resp = self.get_response(source='http://foo.com/%d' % i)
      self.assertEquals(201, resp.status_int, resp.body)

  def test_relative_u_url(self):
    """mf2py expands urls; this just check that we give it the source URL."""
    html = """<article class="h-entry">
<a class="u-url" href="/foo/bar"></a>
<p class="e-content">foo</p></article>"""
    self.expect_requests_get('http://foo.com/bar', html)
    self.mox.ReplayAll()
    self.assert_created('foo - http://foo.com/foo/bar')

  def test_report_error(self):
    """Should report most errors from create() or preview_create()."""
    for i in range(2):
      self.expect_requests_get('http://foo.com/bar', self.post_html % 'foo')

    self.mox.StubOutWithMock(util.error_reporting_client, 'report',
                             use_mock_anything=True)
    for subject in ('WebmentionHandler None failed',
                    'PreviewHandler preview new'):
      util.error_reporting_client.report(subject, http_context=mox.IgnoreArg(),
                                         user=u'http://localhost/fake/foo.com')

    self.mox.StubOutWithMock(self.source.gr_source, 'create',
                             use_mock_anything=True)
    err = exc.HTTPPaymentRequired('fooey')
    self.source.gr_source.create(mox.IgnoreArg(),
                                 include_link=gr_source.INCLUDE_LINK,
                                 ignore_formatting=False
                                 ).AndRaise(err)

    self.mox.StubOutWithMock(self.source.gr_source, 'preview_create',
                             use_mock_anything=True)
    self.source.gr_source.preview_create(mox.IgnoreArg(),
                                         include_link=gr_source.INCLUDE_LINK,
                                         ignore_formatting=False
                                         ).AndRaise(err)

    self.mox.ReplayAll()
    self.assert_error('fooey', status=402)
    self.assertEquals(402, self.get_response(preview=True).status_int)

  def test_silo_500_returns_502(self):
    self.expect_requests_get('http://foo.com/bar', self.post_html % 'xyz')
    self.mox.StubOutWithMock(self.source.gr_source, 'create',
                             use_mock_anything=True)
    err = requests.HTTPError(response=util.Struct(status_code='500', text='foooey bar'))
    self.source.gr_source.create(mox.IgnoreArg(),
                                 include_link=gr_source.INCLUDE_LINK,
                                 ignore_formatting=False
                                 ).AndRaise(err)
    self.mox.ReplayAll()
    self.assert_error('Error: foooey bar', status=502)

  def test_connection_error_returns_504(self):
    self.expect_requests_get('http://foo.com/bar', self.post_html % 'xyz')
    self.mox.StubOutWithMock(self.source.gr_source, 'create',
                             use_mock_anything=True)
    self.source.gr_source.create(mox.IgnoreArg(),
                                 include_link=gr_source.INCLUDE_LINK,
                                 ignore_formatting=False
                                 ).AndRaise(socket.timeout('foooey bar'))
    self.mox.ReplayAll()
    self.assert_error('Error: foooey bar', status=504)

  def test_auth_error_disables_source(self):
    self.expect_requests_get('http://foo.com/bar', self.post_html % 'xyz')
    self.mox.StubOutWithMock(self.source.gr_source, 'create',
                             use_mock_anything=True)
    err = requests.HTTPError(response=requests_response('orig', status=401))
    self.source.gr_source.create(mox.IgnoreArg(),
                                 include_link=gr_source.INCLUDE_LINK,
                                 ignore_formatting=False
                                 ).AndRaise(err)
    self.mox.ReplayAll()

    self.assert_error('orig', status=401)
    self.assertEquals('disabled', self.source.key.get().status)

  def test_non_http_exception(self):
    """If we crash, we shouldn't blame the silo or the user's site."""
    self.expect_requests_get('http://foo.com/bar', self.post_html % 'xyz')
    self.mox.StubOutWithMock(self.source.gr_source, 'create',
                             use_mock_anything=True)
    self.source.gr_source.create(mox.IgnoreArg(),
                                 include_link=gr_source.INCLUDE_LINK,
                                 ignore_formatting=False
                                 ).AndRaise(RuntimeError('baz'))
    self.mox.ReplayAll()
    self.assert_error('500', status=500)

  def test_value_error(self):
    """For example, Twitter raises ValueError on invalid in-reply-to URL....

    ...eg https:/twitter.com/, which matches domain but isn't a tweet.
    """
    self.expect_requests_get('http://foo.com/bar', self.post_html % 'xyz')
    self.mox.StubOutWithMock(self.source.gr_source, 'create',
                             use_mock_anything=True)
    self.source.gr_source.create(mox.IgnoreArg(),
                                 include_link=gr_source.INCLUDE_LINK,
                                 ignore_formatting=False
                                 ).AndRaise(ValueError('baz'))
    self.mox.ReplayAll()
    self.assert_error('baz', status=400)

  def test_preview(self):
    html = self.post_html % 'foo'
    self.expect_requests_get('http://foo.com/bar', html)
    # make sure create() isn't called
    self.mox.StubOutWithMock(self.source.gr_source, 'create', use_mock_anything=True)
    self.mox.ReplayAll()
    self.assert_success('preview of foo', preview=True)

    publish = Publish.query().get()
    self.assertEquals(self.source.key, publish.source)
    self.assertEquals('complete', publish.status)
    self.assertEquals('preview', publish.type)
    self.assertEquals(html + self.backlink, publish.html)

  def test_bridgy_omit_link_query_param(self):
    self.expect_requests_get('http://foo.com/bar', self.post_html % 'foo')
    self.mox.ReplayAll()
    resp = self.assert_created('foo', params={'bridgy_omit_link': 'True'})
    self.assertEquals('foo', json_loads(resp.body)['content'])

  def test_bridgy_omit_link_target_query_param(self):
    self.expect_requests_get('http://foo.com/bar', self.post_html % 'foo')
    self.mox.ReplayAll()

    target = 'https://brid.gy/publish/fake?bridgy_omit_link=true'
    resp = self.assert_created('foo', target=target)
    self.assertEquals('foo', json_loads(resp.body)['content'])

  def test_bridgy_omit_link_mf2(self):
    html = self.post_html % 'foo <a class="u-bridgy-omit-link" href=""></a>'
    self.expect_requests_get('http://foo.com/bar', html)
    self.mox.ReplayAll()
    resp = self.assert_created('foo')
    self.assertEquals('foo', json_loads(resp.body)['content'])

  def test_preview_omit_link_no_query_param_overrides_mf2(self):
    self.expect_requests_get('http://foo.com/bar', self.post_html % 'foo')
    self.mox.ReplayAll()

    resp = self.assert_success('preview of foo', preview=True)
    self.assertIn(
      '<input type="hidden" name="state" value="%7B%22include_link%22%3A%22include%22',
      resp.body.decode('utf-8'))

  def test_preview_omit_link_query_param_overrides_mf2(self):
    html = """\
<article class="h-entry">
<div class="e-content">foo</div>
<a class="u-bridgy-omit-link" href=""></a>
</article>"""
    self.expect_requests_get('http://foo.com/bar', html)
    self.mox.ReplayAll()

    resp = self.assert_success('preview of foo - http://foo.com/bar',
                               preview=True,
                               params={'bridgy_omit_link': 'false'})
    self.assertIn(
      '<input type="hidden" name="state" value="%7B%22include_link%22%3A%22include%22',
      resp.body.decode('utf-8'))

  def test_create_bridgy_omit_link_maybe_query_param(self):
    """Test that ?bridgy_omit_link=maybe query parameter is interpreted
    properly.
    """
    self.expect_requests_get('http://foo.com/bar', self.post_html % 'foo')

    self.mox.StubOutWithMock(
      self.source.gr_source, 'create', use_mock_anything=True)

    self.source.gr_source.create(
      mox.IgnoreArg(), include_link=gr_source.INCLUDE_IF_TRUNCATED,
      ignore_formatting=False
    ).AndReturn(gr_source.creation_result({
      'url': 'http://fake/url',
      'id': 'http://fake/url',
      'content': 'foo',
    }))

    self.mox.ReplayAll()
    self.assert_created('foo', params={'bridgy_omit_link': 'maybe'})

  def test_create_bridgy_omit_link_maybe_mf2(self):
    """Test that bridgy-omit-link=maybe is parsed properly from mf2
    """
    content = '<data class="p-bridgy-omit-link" value="maybe">foo</data>'
    self.expect_requests_get('http://foo.com/bar', self.post_html % content)

    self.mox.StubOutWithMock(
      self.source.gr_source, 'create', use_mock_anything=True)

    self.source.gr_source.create(
      mox.IgnoreArg(), include_link=gr_source.INCLUDE_IF_TRUNCATED,
      ignore_formatting=False
    ).AndReturn(gr_source.creation_result({
      'url': 'http://fake/url',
      'id': 'http://fake/url',
      'content': 'foo',
    }))

    self.mox.ReplayAll()
    self.assert_created('foo')

  def test_bridgy_ignore_formatting_query_param(self):
    self.expect_requests_get('http://foo.com/bar', """\
<article class="h-entry"><div class="e-content">
foo<br /> <blockquote>bar</blockquote>
</div></article>""")
    self.mox.ReplayAll()
    self.assert_created('foo bar', params={'bridgy_ignore_formatting': ''})

  def test_bridgy_ignore_formatting_target_query_param(self):
    self.expect_requests_get('http://foo.com/bar', """\
<article class="h-entry"><div class="e-content">
foo<br /> <blockquote>bar</blockquote>
</div></article>""")
    self.mox.ReplayAll()
    target = 'https://brid.gy/publish/fake?bridgy_ignore_formatting=true'
    self.assert_created('foo bar', target=target)

  def test_bridgy_ignore_formatting_mf2(self):
    self.expect_requests_get('http://foo.com/bar', """\
<article class="h-entry"><div class="e-content">
foo<br /> <blockquote>bar</blockquote>
<a class="u-bridgy-ignore-formatting" href=""></a>
</div></article>""")
    self.mox.ReplayAll()
    self.assert_created('foo bar')

  def test_bridgy_content_query_param_unsupported(self):
    """We originally supported this, then disabled it since it's a security hole.

    https://github.com/snarfed/bridgy/issues/560#issuecomment-161691819
    """
    params = {'bridgy_fake_content': 'use this'}
    self.assert_error('bridgy_fake_content parameter is not supported',
                      params=params)
    self.assert_error('bridgy_fake_content parameter is not supported',
                      preview=True, params=params)

  def test_bridgy_content_mf2(self):
    for i in range(2):
      self.expect_requests_get('http://foo.com/bar', """\
<article class="h-entry">
<div class="e-content">unused</div>
<div class="p-bridgy-fake-content">use this</div>
</article>""")
    self.mox.ReplayAll()

    params = {'bridgy_omit_link': 'false',
              'bridgy_ignore_formatting': 'true'}
    self.assert_success('use this - http://foo.com/bar', preview=True, params=params)
    self.assert_created('use this - http://foo.com/bar', params=params)

  def test_expand_target_urls_u_syndication(self):
    """Comment on a post with a u-syndication value"""
    self.mox.StubOutWithMock(self.source.gr_source, 'create',
                             use_mock_anything=True)

    self.expect_requests_get('http://foo.com/bar', """
    <article class="h-entry">
      <a class="u-url" href="http://foo.com/bar"></a>
      <a class="u-in-reply-to" href="http://orig.domain/baz">In reply to</a>
    </article>
    """)

    self.expect_requests_get('http://orig.domain/baz', """
    <article class="h-entry">
      <span class="p-name e-content">Original post</span>
      <a class="u-syndication" href="https://fa.ke/a/b">syndicated</a>
    </article>
    """)

    self.source.gr_source.create({
      'inReplyTo': [{'url': 'http://orig.domain/baz'},
                    {'url': 'https://fa.ke/a/b'}],
      'displayName': 'In reply to',
      'url': 'http://foo.com/bar',
      'objectType': 'comment',
    }, include_link=gr_source.INCLUDE_LINK, ignore_formatting=False). \
    AndReturn(gr_source.creation_result({
      'url': 'http://fake/url',
      'id': 'http://fake/url',
      'content': 'This is a reply',
    }))

    self.mox.ReplayAll()
    self.assert_created('')

  def test_expand_target_urls_rel_syndication(self):
    """Publishing a like of a post with two rel=syndication values"""
    self.mox.StubOutWithMock(self.source.gr_source, 'create',
                             use_mock_anything=True)

    self.expect_requests_get('http://foo.com/bar', """
    <article class="h-entry">
      <a class="u-url" href="http://foo.com/bar"></a>
      <a class="u-like-of" href="http://orig.domain/baz">liked this</a>
    </article>
    """)

    self.expect_requests_get('http://orig.domain/baz', """
    <link rel="syndication" href="https://fa.ke/a/b">
    <link rel="syndication" href="https://flic.kr/c/d">
    <article class="h-entry">
      <span class="p-name e-content">Original post</span>
    </article>
    """)

    self.source.gr_source.create({
      'verb': 'like',
      'displayName': 'liked this',
      'url': 'http://foo.com/bar',
      'object': [{'url': 'http://orig.domain/baz'},
                 {'url': 'https://fa.ke/a/b'},
                 {'url': 'https://flic.kr/c/d'}],
      'objectType': 'activity',
    }, include_link=gr_source.INCLUDE_LINK, ignore_formatting=False). \
    AndReturn(gr_source.creation_result({
      'url': 'http://fake/url',
      'id': 'http://fake/url',
      'content': 'liked this',
    }))

    self.mox.ReplayAll()
    self.assert_created('')

  def test_expand_target_urls_h_cite(self):
    """Repost a post with a p-syndication h-cite value (syndication
    property is a dict rather than a string)
    """
    self.mox.StubOutWithMock(self.source.gr_source, 'create',
                             use_mock_anything=True)

    self.expect_requests_get('http://foo.com/bar', """
    <article class="h-entry">
      <a class="u-url" href="http://foo.com/bar"></a>
      <a class="u-repost-of" href="http://orig.domain/baz">reposted this</a>
    </article>
    """)

    self.expect_requests_get('http://orig.domain/baz', """
    <article class="h-entry">
      <span class="p-name e-content">Original post</span>
      <a class="p-syndication h-cite" href="https://fa.ke/a/b">On Fa.ke</a>
    </article>
    """)

    self.source.gr_source.create({
      'verb': 'share',
      'displayName': 'reposted this',
      'url': 'http://foo.com/bar',
      'object': [{'url': 'http://orig.domain/baz'},
                 {'url': 'https://fa.ke/a/b'}],
      'objectType': 'activity',
    }, include_link=gr_source.INCLUDE_LINK, ignore_formatting=False). \
    AndReturn(gr_source.creation_result({
      'url': 'http://fake/url',
      'id': 'http://fake/url',
      'content': 'reposted this',
    }))

    self.mox.ReplayAll()
    self.assert_created('')

  def test_expand_target_urls_h_event_in_h_feed(self):
    """RSVP to an event is a single element inside an h-feed; we should handle
    it just like a normal post permalink page.
    """
    self.mox.StubOutWithMock(self.source.gr_source, 'create',
                             use_mock_anything=True)

    self.expect_requests_get('http://foo.com/bar', """
    <article class="h-entry">
      <a class="u-url" href="http://foo.com/bar"></a>
      <a class="u-in-reply-to" href="http://orig.domain/baz"></a>
      <span class="p-rsvp">yes</span>
    </article>
    """)

    self.expect_requests_get('http://orig.domain/baz', """
    <html class="h-feed">
      <article class="h-event">
        <span class="p-name e-content">Original post</span>
        <a class="u-syndication" href="https://fa.ke/a/b">On Fa.ke</a>
      </article>
    </html>
    """)

    self.source.gr_source.create({
      'url': 'http://foo.com/bar',
      'verb': 'rsvp-yes',
      'object': [{'url': 'http://orig.domain/baz'},
                 {'url': 'https://fa.ke/a/b'}],
      'objectType': 'activity',
    }, include_link=gr_source.INCLUDE_LINK, ignore_formatting=False). \
    AndReturn(gr_source.creation_result({
      'url': 'http://fake/url',
      'id': 'http://fake/url',
      'content': 'RSVPd yes',
    }))

    self.mox.ReplayAll()
    self.assert_created('')

  def test_expand_target_urls_fetch_failure(self):
    """Fetching the in-reply-to URL fails, but that shouldn't prevent us
    from publishing the post itself.
    """
    self.mox.StubOutWithMock(self.source.gr_source, 'create',
                             use_mock_anything=True)

    self.expect_requests_get('http://foo.com/bar', """
    <article class="h-entry">
      <a class="u-url" href="http://foo.com/bar"></a>
      <a class="u-in-reply-to" href="http://orig.domain/baz">In reply to</a>
    </article>
    """)

    self.expect_requests_get('http://orig.domain/baz', '', status_code=404)

    self.source.gr_source.create({
      'inReplyTo': [{'url': 'http://orig.domain/baz'}],
      'displayName': 'In reply to',
      'url': 'http://foo.com/bar',
      'objectType': 'comment',
    }, include_link=gr_source.INCLUDE_LINK, ignore_formatting=False). \
    AndReturn(gr_source.creation_result({
      'url': 'http://fake/url',
      'id': 'http://fake/url',
      'content': 'This is a reply',
    }))

    self.mox.ReplayAll()
    self.assert_created('')

  def test_expand_target_urls_no_microformats(self):
    """Publishing a like of a post that has no microformats; should have no
    problems posting the like anyway.
    """
    self.mox.StubOutWithMock(self.source.gr_source, 'create',
                             use_mock_anything=True)

    self.expect_requests_get('http://foo.com/bar', """
    <article class="h-entry">
      <a class="u-url" href="http://foo.com/bar"></a>
      <a class="u-like-of" href="http://orig.domain/baz">liked this</a>
    </article>
    """)

    self.expect_requests_get('http://orig.domain/baz', """
    <article>
      A fantastically well-written article
    </article>
    """)

    self.source.gr_source.create({
      'verb': 'like',
      'displayName': 'liked this',
      'url': 'http://foo.com/bar',
      'object': [{'url': 'http://orig.domain/baz'}],
      'objectType': 'activity',
    }, include_link=gr_source.INCLUDE_LINK, ignore_formatting=False). \
    AndReturn(gr_source.creation_result({
      'url': 'http://fake/url',
      'id': 'http://fake/url',
      'content': 'liked this',
    }))

    self.mox.ReplayAll()
    self.assert_created('')

  def test_expand_target_urls_blacklisted_target(self):
    """RSVP to a domain in the webmention blacklist should not trigger a fetch.
    """
    self.mox.StubOutWithMock(self.source.gr_source, 'create',
                             use_mock_anything=True)

    self.expect_requests_get('http://foo.com/bar', """
    <article class="h-entry">
     <div class="e-content">
      <span class="p-rsvp" value="yes">yes</span>
      <a class="u-in-reply-to" href="http://fa.ke/homebrew-website-club"></a>
     </div>
      <a class="u-url" href="http://foo.com/bar"></a>
    </article>
    """)

    self.source.gr_source.create({
      'url': 'http://foo.com/bar',
      'verb': 'rsvp-yes',
      'object': [{'url': 'http://fa.ke/homebrew-website-club'}],
      'objectType': 'activity',
      'content': '<span class="p-rsvp" value="yes">yes</span>\n<a class="u-in-reply-to" href="http://fa.ke/homebrew-website-club"></a>',
    }, include_link=gr_source.INCLUDE_LINK, ignore_formatting=False). \
    AndReturn(gr_source.creation_result({
      'url': 'http://fake/url',
      'id': 'http://fake/url',
      'content': 'RSVPd yes',
    }))

    self.mox.ReplayAll()
    self.assert_created('')

  def test_in_reply_to_no_target(self):
    """in-reply-to an original that does not syndicate to the silo should
    fail with a helpful error message. The error message is generated by
    granary.
    """
    self.expect_requests_get('http://foo.com/bar', """
    <article class="h-entry">
      <a class="u-url" href="http://foo.com/bar"></a>
      In reply to a post on <a class="u-in-reply-to" href="http://original.domain/baz">original</a>
      <div class="p-name e-content">
        Great post about an important subject
      </div>
    </article>
    """)

    self.expect_requests_get('http://original.domain/baz', """
    <article class="h-entry">
      <div class="p-name e-content">
        boop
      </div>
      <a class="u-syndication" href="http://not-fake/2014">syndicated here</a>
    </article>
    """)

    self.mox.ReplayAll()

    self.assert_error('no fa.ke url to reply to')

  def test_dont_expand_home_page_target_url(self):
    """Replying to a home page shouldn't expand syndication etc. URLs."""
      # <a class="u-url" href="http://foo.com/bar"></a>
    self.expect_requests_get('http://foo.com/bar', """
    <article class="h-entry">
      <div class="p-name e-content">
        Reply to a <a class="u-in-reply-to" href="http://ho.me/">home page</a>
      </div>
    </article>
    """)
    # shouldn't fetch http://ho.me/
    self.mox.ReplayAll()

    self.assert_error('no fa.ke url to reply to')

  def test_html2text(self):
    """Test that using html2text renders whitespace ok in publish content."""
    # based on https://snarfed.org/2014-01-15_homebrew-website-club-tonight
    for i in range(2):
      self.expect_requests_get('http://foo.com/bar', """\
    <article class="h-entry"><div class="e-content">
      <p class="h-event">
      <a class="u-url p-name" href="http://h.w/c">
        Homebrew Website Club</a>
      is <em>tonight</em>!
      <img class="shadow" src="/pour_over_coffee_stand.jpg" /></p>
      <time class="dt-start">6:30pm PST</time> at

      <a href="https://wiki.mozilla.org/SF">Mozilla SF</a> and
      <a href="https://twitter.com/esripdx">Esri Portland</a>.<br />Join us!
    </p></div></article>
    """)

    self.mox.ReplayAll()
    expected = """\
Homebrew Website Club is _tonight_!

6:30pm PST at Mozilla SF and Esri Portland.
Join us!"""

    self.assert_success(expected, preview=True)
    expected += ' - http://foo.com/bar'
    resp = self.assert_created(expected, preview=False)
    self.assertEquals(expected, json_loads(resp.body)['content'])

  def test_unicode(self):
    """Test that we pass through unicode chars correctly."""
    text = 'Démo pour les développeur. Je suis navrée de ce problème.'
    for i in range(2):
      self.expect_requests_get('http://foo.com/bår', self.post_html % text,
                               content_type='text/html; charset=utf-8')
    self.mox.ReplayAll()

    url = 'http://foo.com/bår'.encode('utf-8')
    self.assert_created(text, preview=False, source=url, params={'bridgy_omit_link': ''})
    self.assert_success(text, preview=True, source=url, params={'bridgy_omit_link': ''})

  def test_utf8_meta_tag(self):
    self._test_charset_in_meta_tag('utf-8')

  def test_iso8859_meta_tag(self):
    """https://github.com/snarfed/bridgy/issues/385"""
    self._test_charset_in_meta_tag('iso-8859-1')

  def _test_charset_in_meta_tag(self, charset):
    """Test that we support charset in meta tag as well as HTTP header."""
    text = 'Démo pour les développeur. Je suis navrée de ce problème.'

    resp = requests.Response()
    resp._content = (u"""
<html>
<head><meta charset="%s"></head>
<body><article class="h-entry"><p class="e-content">%s</p></article></body>
<a href="http://localhost/publish/fake"></a>
</html>
""" % (charset, text)).encode(charset)
    resp._text = "shouldn't use this! " + text
    resp.url = 'http://foo.com/bar'
    resp.status_code = 200
    requests.get(resp.url, timeout=appengine_config.HTTP_TIMEOUT,
                 headers=util.REQUEST_HEADERS, stream=True).AndReturn(resp)
    self.mox.ReplayAll()

    self.assert_created(text, params={'bridgy_omit_link': ''})

  def test_missing_backlink(self):
    # use super to avoid this class's override that adds backlink
    super(PublishTest, self).expect_requests_get(
      'http://foo.com/bar', self.post_html % 'foo')
    self.mox.ReplayAll()
    self.assert_error("Couldn't find link to http://localhost/publish/fake")

  def test_facebook_disabled(self):
    self.assert_error('Target must be brid.gy/publish/{',
                      source='http://mr.x/comment',
                      target='https://brid.gy/publish/facebook')

  def test_require_like_of_repost_of(self):
    """We only trigger on like-of and repost-of, not like or repost."""
    for prop in 'like', 'repost':
      url = 'http://foo.com/%s' % prop
      self.expect_requests_get(url, """
      <article class="h-entry">
        <p class="e-content">foo</p>
        <a class="u-url" href="%s"></a>
        <a class="u-%s" href="http://a/like"></a>
      </article>
      """ % (url, prop))

    self.mox.ReplayAll()
    for prop in 'like', 'repost':
      url = 'http://foo.com/%s' % prop
      self.assert_created('foo - %s' % url, source=url)

  def test_unescape(self):
    self.expect_requests_get('http://foo.com/bar', self.post_html % 'abc &amp; xyz')
    self.mox.ReplayAll()
    self.assert_created('abc & xyz - http://foo.com/bar')

  def test_multi_rsvp(self):
    """Test RSVP that replies to multiple event URLs like
    http://tantek.com/2015/308/t1/homebrew-website-club-mozsf
    """
    html = """<div class="h-entry">
    <data class="p-rsvp" value="yes">RSVP yes</data> to:
    <a class="u-in-reply-to h-cite" rel="in-reply-to"
      href="https://kylewm.com/2015/11/sf-homebrew-website-club">
        https://kylewm.com/2015/11/sf-homebrew-website-club
    </a>
    <a class="u-in-reply-to h-cite" rel="in-reply-to"
      href="https://www.facebook.com/events/1510849812560015/">
        https://www.facebook.com/events/1510849812560015/
    </a>
    <p class="p-name e-content">going to Homebrew Website Club 17:30</p>
    <input class="u-url" type="url"
      value="http://tantek.com/2015/308/t1/homebrew-website-club-mozsf" />
    </div>"""

    self.expect_requests_get('http://foo.com/bar', html)
    self.expect_requests_get('https://kylewm.com/2015/11/sf-homebrew-website-club', '')

    # make sure create() isn't called
    self.mox.StubOutWithMock(self.source.gr_source, 'create', use_mock_anything=True)
    self.mox.ReplayAll()
    self.assert_success('going to Homebrew', preview=True)

  def test_multiple_users_on_domain(self):
    source_2 = testutil.FakeSource(
      id='foo.com/b', features=['publish'], domains=['foo.com'],
      domain_urls=['http://foo.com/b'], auth_entity=self.auth_entity.key)
    source_2.put()
    source_3 = testutil.FakeSource(
      id='foo.com/c', features=['publish'], domains=['foo.com'],
      domain_urls=['http://foo.com/c'], auth_entity=self.auth_entity.key)
    source_3.put()

    self.expect_requests_get('http://foo.com/bar', self.post_html % 'foo')
    self.mox.ReplayAll()
    self.assert_created('foo - http://foo.com/bar', interactive=False)
    self.assertEquals(source_2.key, Publish.query().get().source)

  def test_multiple_users_on_domain_no_path_matches(self):
    self.source.domain_urls = ['http://foo.com/a']
    self.source.put()
    source_2 = testutil.FakeSource(
      id='foo.com/c', features=['publish'], domains=['foo.com'],
      domain_urls=['http://foo.com/c'], auth_entity=self.auth_entity.key)
    source_2.put()

    self.assert_error('No account found that matches')

  def test_multiple_users_only_one_registered(self):
    self.source.key.delete()
    source_2 = testutil.FakeSource(
      id='foo.com/b', features=['publish'], domains=['foo.com'],
      auth_entity=self.auth_entity.key)
    source_2.put()
    source_3 = testutil.FakeSource(
      id='foo.com/c', features=['publish'], domains=['foo.com'],
      domain_urls=['http://foo.com/c'], auth_entity=self.auth_entity.key)
    source_3.put()

    self.assert_error('No account found that matches')

  def test_single_user_on_domain_with_wrong_path(self):
    self.source.domain_urls = ['http://foo.com/x']
    self.source.put()
    self.assert_error('No account found that matches')

  def test_dont_escape_period_in_content(self):
    """Odd bug triggered by specific combination of leading <span> and trailing #.

    Root cause was html2text escaping markdown sequences it emits.

    https://github.com/snarfed/bridgy/issues/656
    """
    self.expect_requests_get('http://foo.com/bar',
                             self.post_html % '<span /> 2016. #')
    self.mox.ReplayAll()
    self.assert_created('2016. # - http://foo.com/bar', interactive=False)
    self._check_entity(content='2016. #', html_content='<span /> 2016. #')

  def test_ignore_nested_uphoto(self):
    """We should only use u-photo directly inside the published item.

    ...not u-photos in children, e.g. h-cards.
    """
    for i in range(2):
      self.expect_requests_get('http://foo.com/bar', """
<div class="h-entry">
  <div class="e-content">
    blah
    <div class="h-card">
      <img class="u-photo" src="http://baz.org/img.jpg" />
    </div>
  </div>
</div>
""")
    self.mox.ReplayAll()

    resp = self.assert_created('blah - http://foo.com/bar')
    self.assertNotIn('images', json_loads(resp.body))

    resp = self.assert_success('blah - http://foo.com/bar', preview=True)
    self.assertNotIn('with images', resp.body)

  def test_ignore_jetpack_lazy_loaded_imgs(self):
    """https://github.com/snarfed/bridgy/issues/798"""
    for i in range(2):
      self.expect_requests_get('http://foo.com/bar', """
<div class="h-entry">
<img src="http://example.com/wp-content/plugins/jetpack/modules/lazy-images/images/1x1.trans.gif"
  class="photo u-photo" data-lazy-src="http://example.com/real">
<noscript>
  <img src='http://example.com/real' class='photo u-photo' />
</noscript>
<div class="e-content">blah</div>
</div>
""")
    self.mox.ReplayAll()

    resp = self.assert_created("blah - http://foo.com/bar")
    self.assertEquals(['http://example.com/real'], json_loads(resp.body)['images'])

    resp = self.assert_success('blah - http://foo.com/bar', preview=True)
    self.assertIn('with images http://example.com/real', resp.body)

  def test_nested_h_as_entry(self):
    """https://github.com/snarfed/bridgy/issues/735"""
    self.expect_requests_get('http://foo.com/bar', """
<div class="h-as-entry">
<div class="h-entry">
<p class="e-content">I'M CONTENT</p>
</div></div>
""")
    self.mox.ReplayAll()
    self.assert_error("doesn't support type(s) h-as-entry")

  def test_nested_object_without_url(self):
    """p-repost-of creates an inner object, this one without a u-url.

    From https://dougbeal.com/2017/09/23/instagram-post-by-murbers-%e2%80%a2-sep-23-2017-at-107am-utc/"""
    self.expect_requests_get('http://foo.com/bar', """
<div class="h-entry">
<div class="e-content">

<section class="h-cite p-repost-of">
<blockquote class="e-summary">
<a href="https://www.instagram.com/p/BZXVGQIg_u6/">Doug (@murderofcro.ws) is SOOPER excited about #pelikanhubs2017</a>
</blockquote>
</section>

</div></div>
""")
    self.mox.ReplayAll()
    self.assert_created('Doug (@murderofcro.ws) is SOOPER excited about #pelikanhubs2017')

  def test_not_implemented_error(self):
    """https://github.com/snarfed/bridgy/issues/832"""
    self.expect_requests_get('http://foo.com/bar', """
<div class="h-entry">
<a class="u-in-reply-to" href="http://x/y/z"></a>
<a class="u-tag-of" href="http://a/b/c"></a>
</div>
""")
    self.mox.ReplayAll()
    self.assert_error('Combined in-reply-to and tag-of is not yet supported.')

  def test_delete_not_published_error(self):
    self.expect_requests_get('http://foo.com/bar', status_code=410)
    self.mox.ReplayAll()
    self.assert_error("Can't delete this post from FakeSource because Bridgy Publish didn't originally POSSE it there")

  def test_delete(self):
    page = PublishedPage(id='http://foo.com/bar')
    Publish(parent=page.key, source=self.source.key, status='complete',
            published={'id': 'the_id'}).put()

    for i in range(2):
      self.expect_requests_get('http://foo.com/bar', status_code=410)
    self.mox.ReplayAll()

    resp = self.assert_success('delete the_id', preview=True)
    resp = self.assert_response('', status=302, interactive=True)
    self.assertEquals(
      'http://localhost/fake/foo.com#!'
        'Done! <a href="http://fake/url">Click here to view.</a>',
      urllib.parse.unquote_plus(resp.headers['Location']))

    delete = list(Publish.query())[-1]
    self.assertEquals(delete.key.parent(), page.key)
    self.assertEquals('deleted', delete.status)
    self.assertEquals('delete', delete.type)
    self.assertEquals({
      'id': 'the_id',
      'url': 'http://fake/url',
      'msg': 'delete the_id',
    }, delete.published)

  def test_preview_delete_unsupported_silo(self):
    page = PublishedPage(id='http://foo.com/bar')
    Publish(parent=page.key, source=self.source.key, status='complete',
            published={'id': 'the_id'}).put()

    self.expect_requests_get('http://foo.com/bar', status_code=410)
    self.mox.StubOutWithMock(self.source.gr_source, 'preview_delete',
                             use_mock_anything=True)
    self.source.gr_source.preview_delete(
      mox.IgnoreArg()).AndRaise(NotImplementedError())
    self.mox.ReplayAll()

    self.assert_error("Sorry, deleting isn't supported for FakeSource yet", preview=True)
