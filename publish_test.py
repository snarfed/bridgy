"""Unit tests for publish.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import json
import mox
import urllib

from appengine_config import HTTP_TIMEOUT
import requests

from models import Publish, PublishedPage
import publish
import testutil

from google.appengine.api import mail


class PublishTest(testutil.HandlerTest):

  def setUp(self):
    super(PublishTest, self).setUp()
    publish.SOURCES['fake'] = testutil.FakeSource
    self.source = testutil.FakeSource(
      id='foo.com', features=['publish'], domains=['foo.com'],
      domain_urls=['http://foo.com/'])
    self.source.put()

  def get_response(self, source=None, target=None, preview=False,
                   bridgy_omit_link=None):
    params = {
      'source': source or 'http://foo.com/bar',
      'target': target or 'http://brid.gy/publish/fake',
      }
    if bridgy_omit_link is not None:
      params['bridgy_omit_link'] = bridgy_omit_link

    return publish.application.get_response(
      '/publish/preview' if preview else '/publish/webmention',
      method='POST', body=urllib.urlencode(params))

  def assert_success(self, expected, preview=False, **kwargs):
    resp = self.get_response(preview=preview, **kwargs)
    self.assertEquals(200, resp.status_int)
    body = resp.body if preview else json.loads(resp.body)['content']
    self.assertIn(expected, body)

  def assert_error(self, expected, status=400, **kwargs):
    resp = self.get_response(**kwargs)
    self.assertEquals(status, resp.status_int)
    self.assertIn(expected, json.loads(resp.body)['error'])

  def test_success(self):
    html = '<article class="h-entry"><p class="e-content">foo</p></article>'
    self.expect_requests_get('http://foo.com/bar', html)
    self.mox.ReplayAll()
    self.assert_success('foo - http://foo.com/bar')

    self.assertTrue(PublishedPage.get_by_id('http://foo.com/bar'))
    publish = Publish.query().get()
    self.assertEquals(self.source.key, publish.source)
    self.assertEquals('complete', publish.status)
    self.assertEquals('post', publish.type)
    self.assertEquals('FakeSource post label', publish.type_label)
    self.assertEquals(html, publish.html)
    self.assertEquals({'id': 'fake id', 'url': 'http://fake/url',
                       'content': 'foo - http://foo.com/bar'},
                      publish.published)

  def test_success_domain_translates_to_lowercase(self):
    html = '<article class="h-entry"><p class="e-content">foo</p></article>'
    self.expect_requests_get('http://FoO.cOm/Bar', html)
    self.mox.ReplayAll()
    self.assert_success('foo - http://FoO.cOm/Bar', source='http://FoO.cOm/Bar')

  def test_already_published(self):
    """We shouldn't allow duplicating an existing, *completed* publish."""
    page = PublishedPage(id='http://foo.com/bar')

    # these are all fine
    Publish(parent=page.key, source=self.source.key, status='new').put()
    Publish(parent=page.key, source=self.source.key, status='failed').put()
    Publish(parent=page.key, source=self.source.key, status='complete',
            type='preview').put()

    html = '<article class="h-entry"><p class="e-content">foo</p></article>'
    for i in range(2):
      self.expect_requests_get('http://foo.com/bar', html)
    self.mox.ReplayAll()

    # first attempt should work
    self.assert_success('foo - http://foo.com/bar')
    self.assertEquals(4, Publish.query().count())
    self.assertEquals(2, Publish.query(Publish.status == 'complete').count())

    # now that there's a complete Publish entity, more attempts should fail
    self.assert_error("Sorry, you've already published that page")
    # try again to test for a bug we had where a second try would succeed
    self.assert_error("Sorry, you've already published that page")
    # should still be able to preview though
    self.assert_success('foo - http://foo.com/', preview=True)

  def test_bad_target_url(self):
    self.assert_error('Target must be brid.gy/publish/{facebook,twitter}',
                      target='foo')

  def test_unsupported_source_class(self):
    self.assert_error('Sorry, Google+ is not yet supported.',
                      target='http://brid.gy/publish/googleplus')

  def test_source_url_redirects(self):
    self.expect_requests_head('http://will/redirect', redirected_url='http://foo.com')

    html = '<article class="h-entry"><p class="e-content">foo</p></article>'
    self.expect_requests_get('http://foo.com', html)
    self.mox.ReplayAll()
    # check that we include the original link, not the resolved one
    self.assert_success('foo - http://will/redirect', source='http://will/redirect')

  def test_source_url_redirects_with_refresh_header(self):
    self.mox.StubOutWithMock(requests, 'head', use_mock_anything=True)
    self.expect_requests_head('http://will/redirect',
                              response_headers={'refresh': '0; url=http://foo.com'})
    self.expect_requests_head('http://foo.com')

    html = '<article class="h-entry"><p class="e-content">foo</p></article>'
    self.expect_requests_get('http://foo.com', html)
    self.mox.ReplayAll()
    # check that we include the original link, not the resolved one
    self.assert_success('foo - http://will/redirect', source='http://will/redirect')

  def test_bad_source(self):
    # no source
    msg = 'Could not find <b>FakeSource</b> account for <b>foo.com</b>.'
    self.source.key.delete()
    self.assert_error(msg)

    # source without publish feature
    self.source.features = ['listen']
    self.source.put()
    self.assert_error(msg)

    # status disabled
    self.source.features = ['publish']
    self.source.status = 'disabled'
    self.source.put()
    self.assert_error(msg)

  def test_source_missing_mf2(self):
    self.expect_requests_get('http://foo.com/bar', '')
    self.mox.ReplayAll()
    self.assert_error('No microformats2 data found in http://foo.com/')

    self.assertTrue(PublishedPage.get_by_id('http://foo.com/bar'))
    publish = Publish.query().get()
    self.assertEquals('failed', publish.status)
    self.assertEquals(self.source.key, publish.source)

  def test_no_content(self):
    self.expect_requests_get('http://foo.com/bar',
                             '<article class="h-entry h-as-note"></article>')
    self.mox.ReplayAll()

    self.assert_error('or no content was found')
    self.assertEquals('failed', Publish.query().get().status)

  def test_multiple_items_chooses_first_that_works(self):
    self.expect_requests_get('http://foo.com/bar', """
<a class="h-card" href="http://michael.limiero.com/">Michael Limiero</a>
<article class="h-entry"><p class="e-content">foo bar</article></p>""")
    self.mox.ReplayAll()
    self.assert_success('foo bar - http://foo.com/bar')

  def test_type_not_implemented(self):
    self.expect_requests_get('http://foo.com/bar',
                             '<article class="h-entry h-as-like"></article>')
    self.mox.ReplayAll()

    # FakeSource.create() raises NotImplementedError on likes
    self.assert_error("FakeSource doesn't support type(s) h-as-like")
    self.assertEquals('failed', Publish.query().get().status)

  def test_source_url_is_domain_url(self):
    self.source.put()
    self.assert_error("Looks like that's your home page.", source='https://foo.com#')

    # query params alone shouldn't trigger this
    html = '<article class="h-entry"><p class="e-content">foo</p></article>'
    self.expect_requests_get('http://foo.com/?p=123', html)
    self.mox.ReplayAll()
    self.assert_success('foo - http://foo.com/?p=123',
                        source='http://foo.com/?p=123')

  def test_embedded_type_not_implemented(self):
    self.expect_requests_get('http://foo.com/bar', """
<article class="h-entry">
  <div class="p-like-of">
    foo <a class="u-url" href="http://url">bar</a>
  </div>
</article>""")
    self.mox.ReplayAll()

    # FakeSource.create() raises NotImplementedError on likes
    self.assert_error("FakeSource doesn't support type(s) like-of")
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
    self.assert_success('\nthis is my article\n - http://foo.com/bar')

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
    self.assert_success('this is my article - http://foo.com/bar')

  def test_returned_type_overrides(self):
    # FakeSource returns type 'post' when it sees 'rsvp'
    self.expect_requests_get('http://foo.com/bar', """
<article class="h-entry h-as-rsvp">
<p class="e-content">
<data class="p-rsvp" value="yes"></data>
</p></article>""")
    self.mox.ReplayAll()
    self.assert_success('')
    self.assertEquals('post', Publish.query().get().type)

  def test_in_reply_to_domain_ignores_subdomains(self):
    subdomains = 'www.', 'mobile.', ''
    for i, subdomain in enumerate(subdomains):
      self.expect_requests_get('http://foo.com/%d' % i,
"""<div class="h-entry"><p class="e-content">
<a class="u-in-reply-to" href="http://%sfa.ke/a/b/d">foo</a>
</p></div>""" % subdomain)
    self.mox.ReplayAll()

    for i in range(len(subdomains)):
      resp = self.get_response(source='http://foo.com/%d' % i)
      self.assertEquals(200, resp.status_int, resp.body)

  def test_relative_u_url(self):
    """mf2py expands urls; this just check that we give it the source URL."""
    html = """<article class="h-entry">
<a class="u-url" href="/foo/bar"></a>
<p class="e-content">foo</p></article>"""
    self.expect_requests_get('http://foo.com/bar', html)
    self.mox.ReplayAll()
    self.assert_success('foo - http://foo.com/foo/bar')

  def test_all_errors_email(self):
    """Should send me email on *any* error from create() or preview_create()."""
    html = '<article class="h-entry"><p class="e-content">foo</p></article>'
    for i in range(2):
      self.expect_requests_get('http://foo.com/bar', html)

    self.mox.StubOutWithMock(mail, 'send_mail')
    for subject in ('PublishHandler None failed: None (FakeSource)',
                    'PreviewHandler preview new: None (FakeSource)'):
      mail.send_mail(subject=subject, body=mox.IgnoreArg(),
                     sender=mox.IgnoreArg(), to=mox.IgnoreArg())

    self.mox.StubOutWithMock(self.source.as_source, 'create',
                             use_mock_anything=True)
    self.source.as_source.create(mox.IgnoreArg(), include_link=True
                                 ).AndRaise(Exception('foo'))

    self.mox.StubOutWithMock(self.source.as_source, 'preview_create',
                             use_mock_anything=True)
    self.source.as_source.preview_create(mox.IgnoreArg(), include_link=True
                                         ).AndRaise(Exception('bar'))

    self.mox.ReplayAll()
    self.assert_error('Error: foo', status=500)
    self.assertEquals(500, self.get_response(preview=True).status_int)

  def test_preview(self):
    html = '<article class="h-entry"><p class="e-content">foo</p></article>'
    self.expect_requests_get('http://foo.com/bar', html)
    # make sure create() isn't called
    self.mox.StubOutWithMock(self.source.as_source, 'create', use_mock_anything=True)
    self.mox.ReplayAll()
    self.assert_success('preview of foo - http://foo.com/bar', preview=True)

    publish = Publish.query().get()
    self.assertEquals(self.source.key, publish.source)
    self.assertEquals('complete', publish.status)
    self.assertEquals('preview', publish.type)
    self.assertEquals(html, publish.html)

  def test_bridgy_omit_link_query_param(self):
    html = '<article class="h-entry"><p class="e-content">foo</p></article>'
    self.expect_requests_get('http://foo.com/bar', html)
    self.mox.ReplayAll()
    self.assert_success('foo', bridgy_omit_link='True')

  def test_bridgy_omit_link_mf2(self):
    html = """\
<article class="h-entry">
<p class="e-content">foo</p>
<a class="u-bridgy-omit-link" href=""></a>
</article>"""
    self.expect_requests_get('http://foo.com/bar', html)
    self.mox.ReplayAll()
    self.assert_success('foo', bridgy_omit_link='True')
