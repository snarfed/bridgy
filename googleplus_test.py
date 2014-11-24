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
      id='my_string_id',
      creds_json=json.dumps({'my': 'creds'}),
      user_json=json.dumps({'id': '987',
                            'displayName': 'Mr. G P',
                            'url': 'http://mr/g/p',
                            'image': {'url': 'http://pi.ct/ure?sz=50'},
                            }))
    self.auth_entity.put()
    self.gp = GooglePlusPage.new(self.handler, auth_entity=self.auth_entity)

  def test_new(self):
    self.assertEqual(self.auth_entity, self.gp.auth_entity.get())
    self.assertEqual('987', self.gp.key.string_id())
    self.assertEqual('http://pi.ct/ure?sz=50&sz=128', self.gp.picture)  # overridden sz
    self.assertEqual('Mr. G P', self.gp.name)
    self.assertEqual('http://mr/g/p', self.gp.url)
    self.assertEqual('http://mr/g/p', self.gp.silo_url())

  def test_canonicalize_syndication_url(self):
    self.assertEqual(
      'https://plus.google.com/first.last/1234',
      self.gp.canonicalize_syndication_url('http://plus.google.com/first.last/1234'))

  def test_poll_period(self):
    self.gp.put()
    self.assertEqual(GooglePlusPage.FAST_POLL, self.gp.poll_period())
    self.gp.rate_limited = True
    self.assertEqual(GooglePlusPage.RATE_LIMITED_POLL, self.gp.poll_period())
