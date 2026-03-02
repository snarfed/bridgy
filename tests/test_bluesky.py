"""Unit tests for bluesky.py."""
from unittest import mock
from urllib.parse import parse_qs, urlparse

from mox3 import mox
from oauth_dropins import bluesky as oauth_bluesky
from oauth_dropins.webutil.testutil import requests_response
from oauth_dropins.webutil.util import json_dumps
import requests
from requests_oauth2client import (
  DPoPKey,
  DPoPToken,
  OAuth2AccessTokenAuth,
  TokenSerializer,
)
from werkzeug.routing import RequestRedirect

from bluesky import Bluesky, Callback, OAuthCallback
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

    # https://console.cloud.google.com/errors/detail/CI2OgbfAh-beEA?project=brid-gy
    self.assertIsNone(self.bsky.post_id('https://bsky.app/search?q=https%3A%2F%2Fangadh.com%2Fgraeberandthiel'))

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

  def test_bluesky_client_metadata(self):
    resp = app.test_client().get('/bluesky/client-metadata.json')
    self.assertEqual(200, resp.status_code)
    self.assert_equals({
      'application_type': 'web',
      'client_id': 'http://localhost/bluesky/client-metadata.json',
      'client_name': 'Bridgy',
      'client_uri': 'http://localhost/',
      'dpop_bound_access_tokens': True,
      'grant_types': ['authorization_code', 'refresh_token'],
      'redirect_uris': [
        'http://localhost/bluesky/oauth/callback',
        'http://localhost/bluesky/delete/finish',
        'http://localhost/micropub-token/bluesky/finish',
        'http://localhost/publish/bluesky/finish',
      ],
      'response_types': ['code'],
      'scope': 'atproto transition:generic',
      'token_endpoint_auth_method': 'none',
    }, resp.get_json())

  def test_oauth_callback_add(self):
    self.expect_requests_get('https://alice.com/', '')
    self.mox.ReplayAll()

    state = util.encode_oauth_state({'operation': 'add', 'feature': 'listen'})
    with self.assertRaises(RequestRedirect) as redir, app.test_request_context('/'):
      OAuthCallback('unused').finish(self.auth_entity, state=state)

    location = urlparse(redir.exception.get_response().headers['Location'])
    self.assertEqual('/bluesky/did:web:alice.com', location.path)

  def test_oauth_callback_delete(self):
    self.bsky.features = ['listen', 'publish']
    self.bsky.put()

    state = util.encode_oauth_state({
      'operation': 'delete',
      'feature': 'listen,publish',
    })

    with self.assertRaises(RequestRedirect) as redir, app.test_request_context('/'):
      OAuthCallback('unused').finish(self.auth_entity, state=state)

    location = urlparse(redir.exception.get_response().headers['Location'])
    self.assertEqual('/delete/finish', location.path)
    query = parse_qs(location.query)
    self.assertEqual([self.auth_entity.key.urlsafe().decode()], query['auth_entity'])
    self.assertEqual({
      'operation': 'delete',
      'feature': 'listen,publish',
    }, util.decode_oauth_state(query['state'][0]))

  def test_oauth_callback_no_auth_entity(self):
    with self.assertRaises(RequestRedirect) as redir, app.test_request_context('/'):
      OAuthCallback('unused').finish(None)

    location = urlparse(redir.exception.get_response().headers['Location'])
    self.assertEqual('/', location.path)

  def test_gr_source_oauth(self):
    fake_client = self.mox.CreateMockAnything()
    self.mox.StubOutWithMock(oauth_bluesky, 'oauth_client_for_pds')
    oauth_bluesky.oauth_client_for_pds(
      mox.IgnoreArg(), 'https://bsky.social').AndReturn(fake_client)
    self.mox.ReplayAll()

    dpop_token = DPoPToken(access_token='towkin', _dpop_key=DPoPKey.generate())
    auth_entity = oauth_bluesky.BlueskyAuth(
      id='did:plc:alice',
      pds_url='https://bsky.social',
      dpop_token=TokenSerializer().dumps(dpop_token),
      user_json=json_dumps({
        '$type': 'app.bsky.actor.defs#profileViewDetailed',
        'handle': 'alice.bsky.social',
      }),
    )
    auth_entity.put()
    bsky = Bluesky(id='did:plc:alice', auth_entity=auth_entity.key,
                   username='alice.bsky.social')

    with app.test_request_context('/'):
      gr = bsky.gr_source

    self.assertIsNotNone(gr._client.auth)
    self.assertIsNone(gr._app_password)

  def test_gr_source_oauth_session_callback(self):
    fake_client = self.mox.CreateMockAnything()
    self.mox.StubOutWithMock(oauth_bluesky, 'oauth_client_for_pds')
    oauth_bluesky.oauth_client_for_pds(
      mox.IgnoreArg(), 'https://bsky.social').AndReturn(fake_client)
    self.mox.ReplayAll()

    dpop_token = DPoPToken(access_token='towkin', _dpop_key=DPoPKey.generate())
    auth_entity = oauth_bluesky.BlueskyAuth(
      id='did:plc:alice',
      pds_url='https://bsky.social',
      dpop_token=TokenSerializer().dumps(dpop_token),
      user_json='{}',
    )
    auth_entity.put()

    with app.test_request_context('/'):
      gr = Bluesky(id='did:plc:alice', auth_entity=auth_entity.key).gr_source

    new_token = DPoPToken(access_token='nu_towkin', _dpop_key=DPoPKey.generate())
    auth = OAuth2AccessTokenAuth(client=fake_client, token=new_token)
    gr._client.session_callback(auth)

    self.assertEqual(new_token,
                     TokenSerializer().loads(auth_entity.key.get().dpop_token))
