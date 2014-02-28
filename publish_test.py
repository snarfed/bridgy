"""Unit tests for publish.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import json
import urllib

import appengine_config
import requests

import publish
import testutil


class PublishTest(testutil.HandlerTest):

  def setUp(self):
    super(PublishTest, self).setUp()
    publish.SOURCES['fake'] = testutil.FakeSource
    publish.SUPPORTED_SOURCES.add(testutil.FakeSource)

  def expect_requests_get(self, url, response):
    self.mox.StubOutWithMock(requests, 'get', use_mock_anything=True)
    resp = requests.Response()
    resp._content = response
    requests.get(url).AndReturn(resp)

  def assert_error(self, expected_error, source='http://source',
                   target='http://brid.gy/publish/fake'):
    resp = publish.application.get_response(
      '/publish/webmention', method='POST',
      body='source=%s&target=%s' % (source, target))
    self.assertEquals(400, resp.status_int)
    self.assertEquals(expected_error, json.loads(resp.body)['error'])

  def test_bad_target_url(self):
    self.assert_error('Target must be brid.gy/publish/{facebook,twitter}',
                        target='foo')

  def test_unsupported_source_class(self):
    self.assert_error('Sorry, Instagram is not yet supported.',
                      target='http://brid.gy/publish/instagram')

  def test_bad_source_url(self):
    self.assert_error('Could not parse source URL foo', source='foo')

  def test_source_domain_not_found(self):
    self.assert_error("Could not find FakeSource account for foo.com. Check that you're signed up for Bridgy and that your FakeSource account has foo.com in its profile's 'web site' or 'link' field.",
                      source='http://foo.com/')

  def test_source_missing_mf2(self):
    self.expect_requests_get('http://foo.com/', '')
    self.mox.ReplayAll()

    testutil.FakeSource(id='foo.com', domain='foo.com').put()
    self.assert_error('No microformats2 data found in http://foo.com/',
                      source='http://foo.com/')

  def test_no_content(self):
    self.expect_requests_get('http://foo.com/',
                             '<article class="h-entry h-as-note"></article>')
    self.mox.ReplayAll()

    testutil.FakeSource(id='foo.com', domain='foo.com').put()
    self.assert_error('Could not find e-content in http://foo.com/',
                      source='http://foo.com/')

  def test_type_not_implemented(self):
    self.expect_requests_get('http://foo.com/',
                             '<article class="h-entry h-as-like"></article>')
    self.mox.ReplayAll()

    # FakeSource.create() raises NotImplementedError on likes
    testutil.FakeSource(id='foo.com', domain='foo.com').put()
    self.assert_error("FakeSource doesn't support type(s) ['h-entry', 'h-as-like'].",
                      source='http://foo.com/')

