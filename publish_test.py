"""Unit tests for publish.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import json
import mox
import urllib

import appengine_config
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
    self.source = testutil.FakeSource(id='foo.com', domain='foo.com',
                                      features=['publish'])
    self.source.put()
    self.mox.StubOutWithMock(requests, 'get', use_mock_anything=True)

  def expect_requests_get(self, url, response):
    resp = requests.Response()
    resp._content = response
    resp.url = url
    requests.get(url, allow_redirects=True, timeout=HTTP_TIMEOUT).AndReturn(resp)

  def get_response(self, source=None, target=None, endpoint='/publish/webmention'):
    if source is None:
      source = 'http://foo.com/'
    if target is None:
      target = 'http://brid.gy/publish/fake'
    return publish.application.get_response(
      endpoint, method='POST',
      body='source=%s&target=%s' % (source, target))

  def assert_error(self, expected_error, status=400, **kwargs):
    resp = self.get_response(**kwargs)
    self.assertEquals(status, resp.status_int)
    self.assertIn(expected_error, json.loads(resp.body)['error'])

  def test_success(self):
    html = '<article class="h-entry"><p class="e-content">foo</p></article>'
    self.expect_requests_get('http://foo.com/', html)
    self.mox.ReplayAll()

    resp = self.get_response()
    self.assertEquals(200, resp.status_int, resp.body)
    self.assertEquals('foo - http://foo.com/', json.loads(resp.body)['content'])

    self.assertTrue(PublishedPage.get_by_id('http://foo.com/'))
    publish = Publish.query().get()
    self.assertEquals(self.source.key, publish.source)
    self.assertEquals('complete', publish.status)
    self.assertEquals('post', publish.type)
    self.assertEquals('FakeSource post label', publish.type_label)
    self.assertEquals(html, publish.html)
    self.assertEquals({'id': 'fake id', 'url': 'http://fake/url',
                       'content': 'foo - http://foo.com/'},
                      publish.published)

  def test_success_domain_translates_to_lowercase(self):
    html = '<article class="h-entry"><p class="e-content">foo</p></article>'
    self.expect_requests_get('http://FoO.cOm/', html)
    self.mox.ReplayAll()

    resp = self.get_response(source='http://FoO.cOm/')
    self.assertEquals(200, resp.status_int, resp.body)
    self.assertEquals('foo - http://FoO.cOm/', json.loads(resp.body)['content'])

  def test_already_published(self):
    """We shouldn't allow duplicating an existing, *completed* publish."""
    page = PublishedPage(id='http://foo.com/')

    # these are all fine
    Publish(parent=page.key, source=self.source.key, status='new').put()
    Publish(parent=page.key, source=self.source.key, status='failed').put()
    Publish(parent=page.key, source=self.source.key, status='complete',
            type='preview').put()

    html = '<article class="h-entry"><p class="e-content">foo</p></article>'
    for i in range(2):
      self.expect_requests_get('http://foo.com/', html)
    self.mox.ReplayAll()

    # first attempt should work
    self.assertEquals(200, self.get_response().status_int)
    self.assertEquals(4, Publish.query().count())
    self.assertEquals(2, Publish.query(Publish.status == 'complete').count())

    # now that there's a complete Publish entity, more attempts should fail
    self.assert_error("Sorry, you've already published that page")
    # try again to test for a bug we had where a second try would succeed
    self.assert_error("Sorry, you've already published that page")

    # should still be able to preview though
    resp = self.get_response(endpoint='/publish/preview')
    self.assertEquals(200, resp.status_int, resp.body)
    self.assertIn('foo - http://foo.com/', resp.body, resp.body)

  def test_bad_target_url(self):
    self.assert_error('Target must be brid.gy/publish/{facebook,twitter}',
                      target='foo')

  def test_unsupported_source_class(self):
    self.assert_error('Sorry, Google+ is not yet supported.',
                      target='http://brid.gy/publish/googleplus')

  def test_source_url_redirects(self):
    self.mox.StubOutWithMock(requests, 'head', use_mock_anything=True)
    resp = requests.Response()
    resp.url = 'http://foo.com'
    resp.headers['content-type'] = 'text/html'
    requests.head('http://will/redirect', allow_redirects=True, timeout=HTTP_TIMEOUT
                  ).AndReturn(resp)

    html = '<article class="h-entry"><p class="e-content">foo</p></article>'
    self.expect_requests_get('http://foo.com', html)
    self.mox.ReplayAll()

    resp = self.get_response(source='http://will/redirect')
    self.assertEquals(200, resp.status_int, resp.body)
    # check that we include the original link, not the resolved one
    self.assertEquals('foo - http://will/redirect', json.loads(resp.body)['content'])

  def test_source_url_redirects_with_refresh_header(self):
    self.mox.StubOutWithMock(requests, 'head', use_mock_anything=True)
    resp = requests.Response()
    resp.headers['refresh'] = '0; url=http://foo.com'
    requests.head('http://will/redirect', allow_redirects=True, timeout=HTTP_TIMEOUT
                  ).AndReturn(resp)

    html = '<article class="h-entry"><p class="e-content">foo</p></article>'
    self.expect_requests_get('http://foo.com', html)
    self.mox.ReplayAll()

    resp = self.get_response(source='http://will/redirect')
    self.assertEquals(200, resp.status_int, resp.body)
    # check that we include the original link, not the resolved one
    self.assertEquals('foo - http://will/redirect', json.loads(resp.body)['content'])

  def test_source_domain_not_found(self):
    # no source
    msg = 'Could not find <b>FakeSource</b> account for <b>foo.com</b>.'
    self.source.key.delete()
    self.assert_error(msg)

    # source without publish feature
    self.source.features = ['listen']
    self.source.put()
    self.assert_error(msg)

  def test_source_missing_mf2(self):
    self.expect_requests_get('http://foo.com/', '')
    self.mox.ReplayAll()
    self.assert_error('No microformats2 data found in http://foo.com/')

    self.assertTrue(PublishedPage.get_by_id('http://foo.com/'))
    publish = Publish.query().get()
    self.assertEquals('failed', publish.status)
    self.assertEquals(self.source.key, publish.source)

  def test_no_content(self):
    self.expect_requests_get('http://foo.com/',
                             '<article class="h-entry h-as-note"></article>')
    self.mox.ReplayAll()

    self.assert_error('Could not find e-content in http://foo.com/')
    self.assertEquals('failed', Publish.query().get().status)

  def test_multiple_items_chooses_first_that_works(self):
    self.expect_requests_get('http://foo.com/', """
<a class="h-card" href="http://michael.limiero.com/">Michael Limiero</a>
<article class="h-entry"><p class="e-content">foo bar</article></p>""")
    self.mox.ReplayAll()

    resp = self.get_response()
    self.assertEquals(200, resp.status_int, resp.body)
    self.assertEquals('foo bar - http://foo.com/', json.loads(resp.body)['content'])

  def test_type_not_implemented(self):
    self.expect_requests_get('http://foo.com/',
                             '<article class="h-entry h-as-like"></article>')
    self.mox.ReplayAll()

    # FakeSource.create() raises NotImplementedError on likes
    self.assert_error("FakeSource doesn't support type(s) h-as-like.")
    self.assertEquals('failed', Publish.query().get().status)

  def test_returned_type_overrides(self):
    # FakeSource returns type 'post' when it sees 'rsvp'
    self.expect_requests_get('http://foo.com/', """
<article class="h-entry h-as-rsvp">
<p class="e-content">
<data class="p-rsvp" value="yes"></data>
</p></article>""")
    self.mox.ReplayAll()

    resp = self.get_response()
    self.assertEquals(200, resp.status_int, resp.body)
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
    self.expect_requests_get('http://foo.com/', html)
    self.mox.ReplayAll()

    resp = self.get_response()
    self.assertEquals(200, resp.status_int, resp.body)
    self.assertEquals('foo - http://foo.com/foo/bar',
                      json.loads(resp.body)['content'])

  def test_all_errors_email(self):
    """Should send me email on *any* error from create() or preview_create()."""
    html = '<article class="h-entry"><p class="e-content">foo</p></article>'
    for i in range(2):
      self.expect_requests_get('http://foo.com/', html)

    self.mox.StubOutWithMock(mail, 'send_mail')
    for subject in ('Bridgy publish  failed: None (FakeSource)',
                    'Bridgy publish preview failed: None (FakeSource)'):
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
    self.assertEquals(500, self.get_response(endpoint='/publish/preview').status_int)

  def test_preview(self):
    html = '<article class="h-entry"><p class="e-content">foo</p></article>'
    self.expect_requests_get('http://foo.com/', html)
    # make sure create() isn't called
    self.mox.StubOutWithMock(self.source.as_source, 'create', use_mock_anything=True)
    self.mox.ReplayAll()

    resp = self.get_response(endpoint='/publish/preview')
    self.assertEquals(200, resp.status_int, resp.body)
    self.assertIn('preview of foo - http://foo.com/', resp.body, resp.body)

    publish = Publish.query().get()
    self.assertEquals(self.source.key, publish.source)
    self.assertEquals('complete', publish.status)
    self.assertEquals('preview', publish.type)
    self.assertEquals(html, publish.html)
