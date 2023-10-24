from . import testutil
from bluesky import Bluesky
from oauth_dropins import bluesky as oauth_bluesky
from oauth_dropins.webutil.util import json_dumps
from unittest import mock


class BlueskyTest(testutil.AppTest):

  def fake_lexrpc_call(self, *args, **kwargs):
    return {
      'handle': 'alice.com',
      'did': 'did:web:alice.com',
      'accessJwt': 'towkin'
    }

  def setUp(self):
    super().setUp()
    self.auth_entity = oauth_bluesky.BlueskyAuth(
      id='did:web:alice.com',
      did='did:web:alice.com',
      password='password',
      user_json=json_dumps({
        '$type': 'app.bsky.actor.defs#profileViewDetailed',
        'handle': 'alice.com',
        'displayName': 'Alice',
        'avatar': 'http://pi.ct/ure'
      })
    )
    self.auth_entity.put()
    # Prevent Granary from trying to make a call to Bluesky.
    with mock.patch('lexrpc.Client.call', self.fake_lexrpc_call):
      self.bsky = Bluesky.new(auth_entity=self.auth_entity)

  def test_new(self):
    self.assertEqual(self.auth_entity, self.bsky.auth_entity.get())
    self.assertEqual('alice.com', self.bsky.name)
    self.assertEqual('did:web:alice.com', self.bsky.username)
    self.assertEqual('did:web:alice.com', self.bsky.key.string_id())
    self.assertEqual('alice.com (Bluesky)', self.bsky.label())
    self.assertEqual('https://bsky.app/profile/alice.com', self.bsky.url)
    self.assertEqual('http://pi.ct/ure', self.bsky.picture)
    with mock.patch('lexrpc.Client.call', self.fake_lexrpc_call):
        self.assertEqual('tag:bsky.app,2013:did:web:alice.com', self.bsky.user_tag_id())
        self.assertEqual('https://bsky.app/profile/alice.com', self.bsky.silo_url())
        self.assertEqual('towkin', self.bsky.gr_source.access_token)

  def test_format_for_source_url(self):
    self.assertEqual('at%253A%252F%252Fid', self.bsky.format_for_source_url('at://id'))

  def test_canonicalize_url(self):
    good = 'https://bsky.app/profile/alice.com'
    self.assertEqual(good, self.bsky.canonicalize_url(good))
    self.assertEqual(good, self.bsky.canonicalize_url('http://bsky.app/profile/alice.com'))
