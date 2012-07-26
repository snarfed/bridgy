#!/usr/bin/python
"""Unit tests for wordpress.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import logging
import mox
import testutil
import urllib
import xmlrpclib

import models
import models_test
import wordpress
from wordpress import WordPress, WordPressSite

from google.appengine.ext import webapp


class WordPressBaseTest(mox.MoxTestBase):

  def setUp(self):
    super(WordPressBaseTest, self).setUp()
    self.transport = self.mox.CreateMock(xmlrpclib.Transport)
    WordPress.transport = self.transport
    self.wp = WordPress('http://my/xmlrpc', 999, 'me', 'passwd')
    self.result = [{'foo': 0}, {'bar': 1}]

  def expect_xmlrpc_ok(self, method, *args, **struct):
    self.expect_xmlrpc(method, *args, **struct).AndReturn(self.result)

  def expect_xmlrpc(self, method, *args, **struct):
    args = list(args)
    if struct:
      args.append(struct)
    body = xmlrpclib.dumps(tuple(args), methodname=method)
    return self.transport.request('my', '/xmlrpc', body, verbose=0)


class WordPressTest(WordPressBaseTest):

  def test_get_comments(self):
    self.expect_xmlrpc_ok('wp.getComments', 999, 'me', 'passwd', post_id=123)
    self.mox.ReplayAll()
    self.assertEqual(self.result, self.wp.get_comments(123))

  def test_new_comment(self):
    self.expect_xmlrpc_ok('wp.newComment', 999, '', '', 123,
                       author='me', author_url='http://me', content='foo')
    self.mox.ReplayAll()
    self.assertEqual(self.result, self.wp.new_comment(123, 'me', 'http://me', 'foo'))

  def test_delete_comment(self):
    self.expect_xmlrpc_ok('wp.deleteComment', 999, 'me', 'passwd', 456)
    self.mox.ReplayAll()
    self.assertEqual(self.result, self.wp.delete_comment(456))


class WordPressSiteTest(WordPressBaseTest, testutil.ModelsTest):

  def setUp(self):
    super(WordPressSiteTest, self).setUp()
    self.props = {
      'url': 'http://my/',
      'username': 'me',
      'password': 'my_passwd',
      }
    self.site = WordPressSite(key_name='http://my/xmlrpc_999', **self.props)
    self.user = models.User.get_or_insert_current_user(self.handler)
    self.expected_content = 'foo <cite><a href="http://source/post/url">via FakeSource</a></cite>'


  def test_add_handler(self):
    post_params = dict(self.props)
    post_params['xmlrpc_url'] = 'http://my/xmlrpc'
    self.assertEqual(0, WordPressSite.all().count())

    expected_sites = []
    # if not provided, blog id should default to 0
    for blog_id, expected_blog_id in (('999', 999), ('', 0)):
      post_params['blog_id'] = blog_id
      resp = self.post(wordpress.application, '/wordpress/add', 302,
                       post_params=post_params)
      location = resp.headers['Location']
      self.assertTrue(location.startswith('http://HOST/?'), location)

      key_name = 'http://my/xmlrpc_%d' % expected_blog_id
      expected_sites.append(WordPressSite(key_name=key_name,
                                          owner=models.User.get_current_user(),
                                          **self.props))
      self.assert_entities_equal(expected_sites, WordPressSite.all(),
                                 ignore=['created'])

  def test_add_handler_error(self):
    self.assertEqual(0, WordPressSite.all().count())
    self.props['xmlrpc_url'] = 'http://my/xmlrpc'

    for prop in 'url', 'xmlrpc_url':
      post_params = dict(self.props)
      post_params[prop] = 'not a link'
      resp = self.post(wordpress.application, '/wordpress/add', 302,
                       post_params=post_params)
      location = resp.headers['Location']
      self.assertEqual('http://HOST/?msg=Invalid+URL%3A+not+a+link', location)
      self.assertEqual(0, WordPressSite.all().count())

  def test_delete_handler(self):
    # add a site manually
    post_params = dict(self.props)
    post_params['xmlrpc_url'] = 'http://my/xmlrpc'
    resp = self.post(wordpress.application, '/wordpress/add', 302,
                     post_params=post_params)

    # call the delete handler
    key_name = WordPressSite.all().get().key().name()
    resp = self.post(wordpress.application, '/wordpress/delete', 302,
                     post_params={'name': key_name})
    location = resp.headers['Location']
    self.assertTrue(location.startswith('http://HOST/?'), location)

    self.assertEqual(0, WordPressSite.all().count())

  def test_add_comment(self):
    self.mox.StubOutWithMock(wordpress, 'get_post_id')
    wordpress.get_post_id('http://dest1/post/url').AndReturn(789)

    self.expect_xmlrpc_ok('wp.newComment', 999, '', '', 789,
                       author='me', author_url='http://me',
                       content=self.expected_content)
    self.mox.ReplayAll()
    self.site.add_comment(self.comments[0])

  def test_add_comment_reformat(self):
    """<br /> in comments should be converted to <p />."""
    self.mox.StubOutWithMock(wordpress, 'get_post_id')
    wordpress.get_post_id('http://dest1/post/url').AndReturn(789)

    self.comments[0].content = 'bar<br />foo'
    expected = 'bar<p />' + self.expected_content
    self.expect_xmlrpc_ok('wp.newComment', 999, '', '', 789,
                       author='me', author_url='http://me', content=expected)
    self.mox.ReplayAll()
    self.site.add_comment(self.comments[0])

  def test_add_comment_ignores_500_duplicate_fault(self):
    self.mox.StubOutWithMock(wordpress, 'get_post_id')
    wordpress.get_post_id('http://dest1/post/url').AndReturn(789)

    fault = xmlrpclib.Fault(500, 'Duplicate comment detected!')
    self.expect_xmlrpc('wp.newComment', 999, '', '', 789,
                       author='me', author_url='http://me',
                       content=self.expected_content,
                       ).AndRaise(fault)
    self.mox.ReplayAll()

    self.site.add_comment(self.comments[0])


  def test_add_comment_passes_through_other_fault(self):
    self.mox.StubOutWithMock(wordpress, 'get_post_id')
    wordpress.get_post_id('http://dest1/post/url').AndReturn(789)

    fault = xmlrpclib.Fault(500, 'other error')
    self.expect_xmlrpc('wp.newComment', 999, '', '', 789,
                       author='me', author_url='http://me',
                       content=self.expected_content,
                       ).AndRaise(fault)
    self.mox.ReplayAll()

    self.assertRaises(xmlrpclib.Fault, self.site.add_comment, self.comments[0])
