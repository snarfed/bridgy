# coding=utf-8
"""Unit tests for blogger.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import json
import mox
import urllib
import urlparse

import appengine_config
from appengine_config import HTTP_TIMEOUT
from models import BlogPost

from activitystreams.oauth_dropins.blogger_v2 import BloggerV2Auth
import blogger
from blogger import Blogger
from gdata.blogger import data
from gdata.blogger.client import BloggerClient, Query
from gdata.client import RequestError
import util
import testutil


class BloggerTest(testutil.HandlerTest):

  def setUp(self):
    super(BloggerTest, self).setUp()
    self.auth_entity = BloggerV2Auth(name='name',
                                     blog_ids=['111'],
                                     blog_hostnames=['my.blawg'],
                                     picture_url='http://pic')
    self.client = self.mox.CreateMock(BloggerClient)

    self.comment = data.Comment()
    self.comment.id = util.Struct(
      text='tag:blogger.com,1999:blog-111.post-222.comment-333')
    self.comment.to_string = lambda: '<foo></foo>'

  def expect_get_posts(self):
    post = data.BlogPost()
    post.id = util.Struct(text='tag:blogger.com,1999:blog-111.post-222')
    feed = data.BlogFeed()
    feed.entry = [post]

    def check_path(query):
      return query.custom_parameters['path'] == '/path/to/post'

    self.client.get_posts('111', query=mox.Func(check_path)).AndReturn(feed)

  def test_new(self):
    b = Blogger.new(self.handler, auth_entity=self.auth_entity)
    self.assertEquals(self.auth_entity.key, b.auth_entity)
    self.assertEquals('name', b.name)
    self.assertEquals('http://my.blawg/', b.domain_url)
    self.assertEquals('my.blawg', b.domain)
    self.assertEquals('http://pic', b.picture)

  def test_new_oauth_dropins_error(self):
    """Blogger is special cased in oauth-dropins: when login succeeds but then
    an authenticated API call fails, it returns an empty auth entity key, which
    we can differentiate from a user decline because oauth-dropins can't
    currently intercept Blogger declines.
    """
    resp = blogger.application.get_response('/blogger/add')
    self.assertIn("Couldn't fetch your blogs",
        urllib.unquote(urlparse.urlparse(resp.headers['Location']).fragment))
    self.assertEquals(0, BloggerV2Auth.query().count())
    self.assertEquals(0, Blogger.query().count())

  def test_new_no_blogs(self):
    self.auth_entity.blog_hostnames = []
    self.assertIsNone(Blogger.new(self.handler, auth_entity=self.auth_entity))
    self.assertIn('No Blogger blogs found', next(iter(self.handler.messages)))

  def test_create_comment(self):
    self.expect_get_posts()
    self.client.add_comment('111', '222', '<a href="http://who">who</a>: foo bar'
                            ).AndReturn(self.comment)
    self.mox.ReplayAll()

    b = Blogger.new(self.handler, auth_entity=self.auth_entity)
    resp = b.create_comment('http://blawg/path/to/post', 'who', 'http://who',
                            'foo bar', client=self.client)
    self.assert_equals({'id': '333', 'response': '<foo></foo>'}, resp)

  def test_create_comment_with_unicode_chars(self):
    # TODO: this just checks the arguments passed to client.add_comment(). we
    # should test that the blogger client itself encodes as UTF-8.
    self.expect_get_posts()
    self.client.add_comment(
      '111', '222', u'<a href="http://who">Degenève</a>: foo Degenève bar'
      ).AndReturn(self.comment)
    self.mox.ReplayAll()

    b = Blogger.new(self.handler, auth_entity=self.auth_entity)
    resp = b.create_comment('http://blawg/path/to/post', u'Degenève', 'http://who',
                            u'foo Degenève bar', client=self.client)

  def test_create_comment_gives_up_on_internal_error_bX2i87au(self):
    # see https://github.com/snarfed/bridgy/issues/175
    self.expect_get_posts()
    self.client.add_comment('111', '222', '<a href="http://who">who</a>: foo bar'
                            ).AndRaise(RequestError('500, Internal error: bX-2i87au'))
    self.mox.ReplayAll()

    b = Blogger.new(self.handler, auth_entity=self.auth_entity)
    resp = b.create_comment('http://blawg/path/to/post', 'who', 'http://who',
                            'foo bar', client=self.client)
    # the key point is that create_comment doesn't raise an exception
    self.assert_equals({'error': '500, Internal error: bX-2i87au'}, resp)

  def test_superfeedr_notify(self):
    """Smoke test. Just check that we make it all the way through."""
    Blogger.new(self.handler, auth_entity=self.auth_entity).put()
    resp = blogger.application.get_response(
      '/blogger/notify/111', method='POST', body=json.dumps({'items': []}))
    self.assertEquals(200, resp.status_int)
