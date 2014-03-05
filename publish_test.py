"""Unit tests for publish.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import json
import urllib

import appengine_config
from appengine_config import HTTP_TIMEOUT
import requests

import models
import publish
import testutil


class PublishTest(testutil.HandlerTest):

  def setUp(self):
    super(PublishTest, self).setUp()
    publish.SOURCES['fake'] = testutil.FakeSource
    self.source = testutil.FakeSource(id='foo.com', domain='foo.com')
    self.source.put()

  def expect_requests_get(self, url, response):
    self.mox.StubOutWithMock(requests, 'get', use_mock_anything=True)
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

  def assert_error(self, expected_error, source=None, target=None):
    resp = self.get_response(source=source, target=target)
    self.assertEquals(400, resp.status_int)
    self.assertEquals(expected_error, json.loads(resp.body)['error'])

  def test_success(self):
    html = '<article class="h-entry"><p class="e-content">foo</p></article>'
    self.expect_requests_get('http://foo.com/', html)
    self.mox.ReplayAll()

    resp = self.get_response()
    self.assertEquals(200, resp.status_int, resp.body)
    self.assertEquals('foo\n\n(http://foo.com/)', json.loads(resp.body)['content'])

    self.assertTrue(models.PublishedPage.get_by_id('http://foo.com/'))
    publish = models.Publish.query().get()
    self.assertEquals(self.source.key, publish.source)
    self.assertEquals('complete', publish.status)
    self.assertEquals('post', publish.type)
    self.assertEquals(html, publish.html)
    self.assertEquals({'id': 'fake id', 'url': 'http://fake/url',
                       'content': 'foo\n\n(http://foo.com/)'},
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
    testutil.FakeSource.get_by_id('foo.com').key.delete()
    self.assert_error("Could not find FakeSource account for foo.com. Check that you're signed up for Bridgy and that your FakeSource account has foo.com in its profile's 'web site' or 'link' field.")

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

    self.assert_error("Could not find FakeSource link.")
    self.assertEquals('failed', models.Publish.query().get().status)

  def test_preview(self):
    html = '<article class="h-entry"><p class="e-content">foo</p></article>'
    self.expect_requests_get('http://foo.com/', html)
    # make sure create() isn't called
    self.mox.StubOutWithMock(self.source.as_source, 'create', use_mock_anything=True)
    self.mox.ReplayAll()

    resp = self.get_response(endpoint='/publish/preview')
    self.assertEquals(200, resp.status_int, resp.body)
    self.assertTrue(resp.body.endswith(
        'preview of foo\n\n(<a href="http://foo.com/">http://foo.com/</a>)'),
                    resp.body)

