"""Unit tests for mastodon.py.
"""
from __future__ import absolute_import, unicode_literals
from future import standard_library
standard_library.install_aliases()

import appengine_config
from oauth_dropins import mastodon as oauth_mastodon
from oauth_dropins.webutil.util import json_dumps, json_loads

from . import testutil
from mastodon import Mastodon


class MastodonTest(testutil.ModelsTest):

  def setUp(self):
    super(MastodonTest, self).setUp()
    self.handler.messages = []
    self.auth_entity = oauth_mastodon.MastodonAuth(
      id='me@foo.com', access_token='towkin', user_json=json_dumps({
        'id': '123',
        'username': 'me',
        'acct': 'me',
        'url': 'https://foo.com/@me',
        'display_name': 'Ryan Barrett',
        'avatar': 'http://pi.ct/ure',
      }))
    self.auth_entity.put()
    self.m = Mastodon.new(self.handler, auth_entity=self.auth_entity)

  def test_new(self):
    self.assertEqual(self.auth_entity, self.m.auth_entity.get())
    self.assertEqual('towkin', self.m.gr_source.access_token)
    self.assertEqual('me@foo.com', self.m.key.string_id())
    self.assertEqual('http://pi.ct/ure', self.m.picture)
    self.assertEqual('Ryan Barrett', self.m.name)
    self.assertEqual('https://foo.com/me', self.m.url)
    self.assertEqual('https://foo.com/me', self.m.silo_url())
    self.assertEqual('tag:foo.com,2013:me', self.m.user_tag_id())
    self.assertEqual('me@foo.com (Mastodon)', self.m.label())

  def test_canonicalize_url(self):
    good = 'https://foo.com/x/status/123'
    self.assertEqual(good, self.m.canonicalize_url(good))
    self.assertEqual(good, self.m.canonicalize_url(
      'http://foo.com/x/status/123/'))
    self.assertIsNone(self.m.canonicalize_url(
      'https://foo.com/x?protected_redirect=true'))

  def test_is_private(self):
    self.assertFalse(self.m.is_private())

    self.auth_entity.user_json = json_dumps({'locked': True})
    self.auth_entity.put()
    self.assertTrue(self.m.is_private())
