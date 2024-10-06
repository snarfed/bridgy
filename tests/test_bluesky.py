"""Unit tests for bluesky.py."""
from unittest import mock
from urllib.parse import parse_qs, urlparse

from oauth_dropins import bluesky as oauth_bluesky
from oauth_dropins.webutil.testutil import requests_response
from oauth_dropins.webutil.util import json_dumps
import requests
from werkzeug.routing import RequestRedirect

from bluesky import Bluesky, Callback
from flask_app import app
from models import DisableSource
import util
from . import testutil


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

  def test_delete(self):
    self.bsky.features = ['listen', 'publish']
    self.bsky.put()

    with self.assertRaises(RequestRedirect) as redir, app.test_request_context(data={
        'operation': 'delete',
        'feature': 'listen,publish',
      }):
      Callback('unused').finish(self.auth_entity)

    location = urlparse(redir.exception.get_response().headers['Location'])
    self.assertEqual('/delete/finish', location.path)
    query = parse_qs(location.query)
    self.assertEqual([self.auth_entity.key.urlsafe().decode()], query['auth_entity'])
    self.assertEqual({
      'operation': 'delete',
      'feature': 'listen,publish',
      'source': self.bsky.key.urlsafe().decode(),
    }, util.decode_oauth_state(query['state'][0]))

  @mock.patch('requests.get', return_value=requests_response({'feed': []}))
  def test_get_activities(self, _):
    self.assertEqual([], self.bsky.get_activities())

  @mock.patch('requests.get', return_value=requests_response({}, status=400))
  def test_get_activities_error(self, _):
    with self.assertRaises(requests.HTTPError):
      self.bsky.get_activities()

  @mock.patch('requests.post')
  @mock.patch('requests.get')
  def test_get_activities_token_error(self, mock_get, mock_post):
    mock_get.return_value = mock_post.return_value = requests_response({
      'error': 'ExpiredToken',
      'message': 'Token has been revoked',
    }, status=400)

    with self.assertRaises(DisableSource):
      self.bsky.get_activities()
