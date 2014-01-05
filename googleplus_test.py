"""Unit tests for googleplus.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import json
import testutil

from activitystreams.oauth_dropins import googleplus as oauth_googleplus
from googleplus import GooglePlusPage


class GooglePlusTest(testutil.ModelsTest):

  def setUp(self):
    super(GooglePlusTest, self).setUp()
    self.handler.messages = []
    self.auth_entity = oauth_googleplus.GooglePlusAuth(
      key_name='my_key_name',
      creds_json=json.dumps({'my': 'creds'}),
      user_json=json.dumps({'id': '987',
                            'displayName': 'Mr. G P',
                            'url': 'http://mr/g/p',
                            'image': {'url': 'http://pi.ct/ure'},
                            }))

  def test_new(self):
    gp = GooglePlusPage.new(self.handler, auth_entity=self.auth_entity)
    self.assertEqual(self.auth_entity, gp.auth_entity)
    self.assertEqual('987', gp.key().name())
    self.assertEqual('http://pi.ct/ure', gp.picture)
    self.assertEqual('Mr. G P', gp.name)
