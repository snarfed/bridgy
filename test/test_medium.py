# coding=utf-8
"""Unit tests for medium.py.
"""

import json

import appengine_config
from oauth_dropins.medium import MediumAuth

import testutil
from medium import Medium


class MediumTest(testutil.HandlerTest):

  def setUp(self):
    super(MediumTest, self).setUp()
    self.auth_entity = MediumAuth(
      id='abcdef01234', access_token_str='my token', user_json=json.dumps({
        'data': {
          'id': 'abcdef01234',
          'username': 'ry',
          'name': 'Ryan',
          'url': 'http://my/blog',
          'imageUrl': 'http://ava/tar',
          },
        }))
    self.auth_entity.put()

  def test_new(self):
    m = Medium.new(self.handler, auth_entity=self.auth_entity)
    self.assertEquals(self.auth_entity.key, m.auth_entity)
    self.assertEquals('@ry', m.key.id())
    self.assertEquals('Ryan', m.name)
    self.assertEquals('http://my/blog', m.url)
    self.assertEquals('http://ava/tar', m.picture)
