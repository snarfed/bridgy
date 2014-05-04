"""Unit tests for blogger.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import json
import mox

import appengine_config
from appengine_config import HTTP_TIMEOUT
from models import BlogPost

from activitystreams.oauth_dropins import blogger_v2 as oauth_blogger
import blogger
from blogger import Blogger
from gdata.blogger import data
from gdata.blogger.client import BloggerClient, Query
import util
import testutil


class BloggerTest(testutil.HandlerTest):

  def setUp(self):
    super(BloggerTest, self).setUp()
    self.auth_entity = oauth_blogger.BloggerV2Auth(
      name='name',
      blog_ids=['111'],
      blog_hostnames=['my.blawg'],
      picture_url='http://pic')
    self.client = self.mox.CreateMock(BloggerClient)

  def test_new(self):
    b = Blogger.new(self.handler, auth_entity=self.auth_entity)
    self.assertEquals(self.auth_entity.key, b.auth_entity)
    self.assertEquals('name', b.name)
    self.assertEquals('http://my.blawg/', b.domain_url)
    self.assertEquals('my.blawg', b.domain)
    self.assertEquals('http://pic', b.picture)

  def test_new_no_blogs(self):
    self.auth_entity.blog_hostnames = []
    self.assertIsNone(Blogger.new(self.handler, auth_entity=self.auth_entity))
    self.assertIn('No Blogger blogs found', next(iter(self.handler.messages)))

  def test_create_comment(self):
    post = data.BlogPost()
    post.id = util.Struct(text='tag:blogger.com,1999:blog-111.post-222')
    feed = data.BlogFeed()
    feed.entry = [post]

    def check_path(query):
      return query.custom_parameters['path'] == '/path/to/post'

    self.client.get_posts('111', query=mox.Func(check_path)
                          ).AndReturn(feed)
    self.client.add_comment('111', '222', '<a href="http://who">who</a>: foo bar'
                            ).AndReturn({})
    self.mox.ReplayAll()

    b = Blogger.new(self.handler, auth_entity=self.auth_entity)
    b.create_comment('http://blawg/path/to/post', 'who', 'http://who', 'foo bar',
                     client=self.client)
