"""Unit tests for instagram.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import json
import logging
import testutil

from activitystreams import instagram_test as as_instagram_test
from activitystreams import source
from activitystreams.oauth_dropins import instagram as oauth_instagram
from instagram import Instagram
from webutil import util


class InstagramTest(testutil.ModelsTest):

  def setUp(self):
    super(InstagramTest, self).setUp()
    self.handler.messages = []
    self.auth_entity = oauth_instagram.InstagramAuth(
      key_name='my_key_name', auth_code='my_code', access_token_str='my_token',
      user_json=json.dumps({'username': 'snarfed',
                            'full_name': 'Ryan Barrett',
                            'bio': 'something about me',
                            'profile_picture': 'http://pic.ture/url',
                            }))

  def test_new(self):
    inst = Instagram.new(self.handler, auth_entity=self.auth_entity)
    self.assertEqual(self.auth_entity, inst.auth_entity)
    self.assertEqual('my_token', inst.as_source.access_token)
    self.assertEqual('snarfed', inst.key().name())
    self.assertEqual('http://pic.ture/url', inst.picture)
    self.assertEqual('http://instagram.com/snarfed', inst.url)
    self.assertEqual('Ryan Barrett', inst.name)

  def test_get_activities_response(self):
    """Check that min_id is discarded."""
    inst = Instagram.new(self.handler, auth_entity=self.auth_entity)
    self.mox.StubOutWithMock(inst.as_source.api, 'user_recent_media')
    inst.as_source.api.user_recent_media('self').AndReturn(([], {}))
    self.mox.ReplayAll()
    inst.get_activities_response(group_id=source.SELF, min_id='123')
