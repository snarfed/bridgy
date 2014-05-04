"""Unit tests for wordpress_rest.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import json
import mox

import appengine_config
from appengine_config import HTTP_TIMEOUT
from models import BlogPost

from activitystreams.oauth_dropins.wordpress_rest import WordPressAuth
# import wordpress_rest
from wordpress_rest import WordPress
import testutil


class WordPressTest(testutil.HandlerTest):

  def setUp(self):
    super(WordPressTest, self).setUp()
    self.auth_entity = WordPressAuth(id='my.wp.com',
                                     user_json=json.dumps({
                                       'display_name': 'Ryan',
                                       'username': 'ry',
                                       'avatar_URL': 'http://ava/tar'}),
                                     blog_id='123',
                                     blog_url='http://my.wp.com/')

  def test_new(self):
    w = WordPress.new(self.handler, auth_entity=self.auth_entity)
    self.assertEquals(self.auth_entity.key, w.auth_entity)
    self.assertEquals('my.wp.com', w.key.id())
    self.assertEquals('Ryan', w.name)
    self.assertEquals('http://my.wp.com/', w.domain_url)
    self.assertEquals('my.wp.com', w.domain)
    self.assertEquals('http://ava/tar', w.picture)
