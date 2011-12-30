#!/usr/bin/python
"""Unit tests for wordpress.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import logging
import mox
import testutil
import xmlrpclib

import wordpress
from wordpress import WordPress, WordPressSite
import models
import models_test


class WordPressBaseTest(mox.MoxTestBase):

  def setUp(self):
    super(WordPressBaseTest, self).setUp()
    self.transport = self.mox.CreateMock(xmlrpclib.Transport)
    WordPress.transport = self.transport
    self.wp = WordPress('http://my/xmlrpc', 999, 'me', 'my_passwd')
    self.result = [{'foo': 0}, {'bar': 1}]

  def expect_xmlrpc(self, method,params):
    body = xmlrpclib.dumps((999, 'me', 'my_passwd', params), methodname=method)
    self.transport.request('my', '/xmlrpc', body, verbose=0).AndReturn(self.result)


class WordPressTest(WordPressBaseTest):

  def test_get_comments(self):
    self.expect_xmlrpc('wp.getComments', {'post_id': 123})
    self.mox.ReplayAll()
    self.assertEqual(self.result, self.wp.get_comments(123))

  def test_new_comment(self):
    self.expect_xmlrpc('wp.newComment', {'post_id': 123, 'author': 'me',
                                         'author_url': 'http://me', 'content': 'foo'})
    self.mox.ReplayAll()
    self.assertEqual(self.result, self.wp.new_comment(123, 'me', 'http://me', 'foo'))
                    
  def test_delete_comment(self):
    self.expect_xmlrpc('wp.deleteComment', {'comment_id': 456})
    self.mox.ReplayAll()
    self.assertEqual(self.result, self.wp.delete_comment(456))


class WordPressSiteTest(WordPressBaseTest, models_test.ModelsTest):

  def setUp(self):
    super(WordPressSiteTest, self).setUp()
    self.props = {
      'username': 'me',
      'password': 'my_passwd',
      }
    self.site = WordPressSite(key_name='http://my/xmlrpc_999', **self.props)
    self.user = models.User.get_or_insert_current_user(self.handler)

  def test_new(self):
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
      expected_sites.append(WordPressSite(key_name=key_name, **self.props))
      self.assert_entities_equal(expected_sites, WordPressSite.all(),
                                 ignore=['created'])
      self.assertEqual(self.entity_keys(expected_sites),
                       models.User.get_current_user().dests)

  def test_delete(self):
    self.assertEqual(0, WordPressSite.all().count())

    # add a site manually
    params = dict(self.props)
    params['xmlrpc_url'] = 'http://my/xmlrpc'
    site = WordPressSite.new(params, self.handler)
    self.assertEqual(1, WordPressSite.all().count())

    # call the delete handler
    resp = self.post(wordpress.application, '/wordpress/delete', 302,
                     post_params={'name': site.key().name()})
    location = resp.headers['Location']
    self.assertTrue(location.startswith('http://HOST/?'), location)

    self.assertEqual(0, WordPressSite.all().count())

  def test_add_comment(self):
    self.mox.StubOutWithMock(wordpress, 'get_post_id')
    wordpress.get_post_id('http://dest/post/url').AndReturn(789)
    # WordPress.new_comment(mox.IgnoreArg(), 123, 'me', 'http://me', content)

    content = """foo
<a href="http://source/post/url">(from FakeSource)</a>"""
    args = {'post_id': 789,
            'author': 'me',
            'author_url': 'http://me',
            'content': content,
            }
    self.expect_xmlrpc('wp.newComment', args)

    self.mox.ReplayAll()
    self.site.add_comment(self.comments[0])
