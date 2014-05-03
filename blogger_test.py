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
import testutil


class BloggerTest(testutil.HandlerTest):

  def setUp(self):
    super(BloggerTest, self).setUp()
    self.auth_entity = oauth_blogger.BloggerV2Auth(
      name='name',
      blog_ids=['111'],
      blog_hostnames=['my.blawg'],
      picture_url='http://pic')

  def test_new(self):
    b = Blogger.new(self.handler, auth_entity=self.auth_entity)
    self.assertEquals(self.auth_entity.key, b.auth_entity)
    self.assertEquals('name', b.name)
    self.assertEquals('http://my.blawg/', b.domain_url)
    self.assertEquals('my.blawg', b.domain)
    self.assertEquals('http://pic', b.picture)
