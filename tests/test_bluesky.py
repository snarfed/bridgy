from . import testutil
from bluesky import Bluesky
from oauth_dropins import bluesky as oauth_bluesky
from oauth_dropins.webutil.util import json_dumps
from unittest import mock


class BlueskyTest(testutil.AppTest):

  def setUp(self):
    super().setUp()
    self.auth_entity = oauth_bluesky.BlueskyAuth(
      id='did:web:alice.com',
      password='password',
      user_json=json_dumps({
        '$type': 'app.bsky.actor.defs#profileViewDetailed',
        'handle': 'alice.com',
        'displayName': 'Alice',
        'avatar': 'http://pi.ct/ure'
      }),
      session={
        'handle': 'alice.com',
        'did': 'did:web:alice.com',
        'accessJwt': 'towkin',
        'refreshJwt': 'reephrush',
      },
    )
    self.auth_entity.put()
    self.bsky = Bluesky.new(auth_entity=self.auth_entity)

  def test_new(self):
    self.assertEqual(self.auth_entity, self.bsky.auth_entity.get())
    self.assertEqual('Alice', self.bsky.name)
    self.assertEqual('alice.com', self.bsky.username)
    self.assertEqual('did:web:alice.com', self.bsky.key.string_id())
    self.assertEqual('alice.com (Bluesky)', self.bsky.label())
    self.assertEqual('alice.com', self.bsky.label_name())
    self.assertEqual('https://bsky.app/profile/alice.com', self.bsky.url)
    self.assertEqual('http://pi.ct/ure', self.bsky.picture)

    self.assertEqual('tag:bsky.app,2013:did:web:alice.com', self.bsky.user_tag_id())
    self.assertEqual('https://bsky.app/profile/alice.com', self.bsky.silo_url())
    self.assertEqual({
      'accessJwt': 'towkin',
      'refreshJwt': 'reephrush',
    }, self.bsky.gr_source.client.session)

  def test_format_for_source_url(self):
    self.assertEqual('at%253A%252F%252Fid', self.bsky.format_for_source_url('at://id'))

  def test_post_id(self):
    good = 'at://did:web:alice.com/app.bsky.feed.post/123'
    self.assertEqual(good, self.bsky.post_id(good))
    self.assertEqual(good, self.bsky.post_id('at://alice.com/app.bsky.feed.post/123'))
    self.assertEqual(good, self.bsky.post_id('https://bsky.app/profile/alice.com/post/123'))

  def test_canonicalize_url(self):
    for input, expected in [
        ('https://bsky.app/foo', 'https://bsky.app/foo'),
        ('http://bsky.app/foo', 'https://bsky.app/foo'),
        ('https://staging.bsky.app/foo', 'https://bsky.app/foo'),
        ('at://did:web:alice.com', 'https://bsky.app/profile/alice.com'),
        ('at://did:web:alice.com/app.bsky.feed.post/123', 'https://bsky.app/profile/alice.com/post/123'),
    ]:
      self.assertEqual(expected, self.bsky.canonicalize_url(input))
