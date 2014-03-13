"""Unit tests for publish.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import json
import mox
import urllib

import appengine_config
from appengine_config import HTTP_TIMEOUT
import requests

import models
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
    self.assertEquals(expected_error, json.loads(resp.body)['error'])

  def test_success(self):
    html = '<article class="h-entry"><p class="e-content">foo</p></article>'
    self.expect_requests_get('http://foo.com/', html)
    self.mox.ReplayAll()

    resp = self.get_response()
    self.assertEquals(200, resp.status_int, resp.body)
    self.assertEquals('foo - http://foo.com/', json.loads(resp.body)['content'])

    self.assertTrue(models.PublishedPage.get_by_id('http://foo.com/'))
    publish = models.Publish.query().get()
    self.assertEquals(self.source.key, publish.source)
    self.assertEquals('complete', publish.status)
    self.assertEquals('post', publish.type)
    self.assertEquals(html, publish.html)
    self.assertEquals({'id': 'fake id', 'url': 'http://fake/url',
                       'content': 'foo - http://foo.com/'},
                      publish.published)

  def test_bad_target_url(self):
    self.assert_error('Target must be brid.gy/publish/{facebook,twitter}',
                      target='foo')

  def test_unsupported_source_class(self):
    self.assert_error('Sorry, Google+ is not yet supported.',
                      target='http://brid.gy/publish/googleplus')

  def test_bad_source_url(self):
    self.assert_error('Could not parse source URL foo', source='foo')

  def test_source_domain_not_found(self):
    msg = "Could not find FakeSource account for foo.com. Check that you're signed up for Bridgy Publish and that your FakeSource account has foo.com in its profile's 'web site' or 'link' field."

    # no source
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

    self.assertTrue(models.PublishedPage.get_by_id('http://foo.com/'))
    publish = models.Publish.query().get()
    self.assertEquals('failed', publish.status)
    self.assertEquals(self.source.key, publish.source)

  def test_no_content(self):
    self.expect_requests_get('http://foo.com/',
                             '<article class="h-entry h-as-note"></article>')
    self.mox.ReplayAll()

    self.assert_error('Could not find e-content in http://foo.com/')
    self.assertEquals('failed', models.Publish.query().get().status)

  def test_type_not_implemented(self):
    self.expect_requests_get('http://foo.com/',
                             '<article class="h-entry h-as-like"></article>')
    self.mox.ReplayAll()

    # FakeSource.create() raises NotImplementedError on likes
    self.assert_error("FakeSource doesn't support type(s) h-as-like.")
    self.assertEquals('failed', models.Publish.query().get().status)

  def test_in_reply_to_domain_mismatch(self):
    self.expect_requests_get('http://foo.com/', """
<article class="h-entry h-as-reply">
<p class="e-content">
<a class="u-in-reply-to" href="http://other/silo">foo</a>
</p></article>""")
    self.mox.ReplayAll()

    self.assert_error("Could not find FakeSource link in http://foo.com/")
    self.assertEquals('failed', models.Publish.query().get().status)

  def test_all_errors_email(self):
    """Should send me email on *any* error from create() or preview_create()."""
    html = '<article class="h-entry"><p class="e-content">foo</p></article>'
    for i in range(2):
      self.expect_requests_get('http://foo.com/', html)

    self.mox.StubOutWithMock(mail, 'send_mail')
    for subject in ('Bridgy publish failed: None (FakeSource)',
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
    self.assertTrue('preview of foo - http://foo.com/' in resp.body, resp.body)
