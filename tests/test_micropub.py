"""Unit tests for micropub.py."""
import html
from io import BytesIO
import urllib.request, urllib.parse, urllib.error

from flask import get_flashed_messages
from werkzeug.datastructures import MultiDict

from flask_app import app
import micropub
from models import Publish, PublishedPage
from .testutil import AppTest, FakeAuthEntity, FakeSource
import util


class FakeToken(micropub.GetToken):
  # populated in setUp()
  auth_entity = None
  oauth_state = None

  def dispatch_request(self):
    return self.finish(self.auth_entity, state=self.oauth_state)

app.add_url_rule('/micropub-token/fake/finish',
                 view_func=FakeToken.as_view('test_micropub_token_fake'))


class MicropubTest(AppTest):

  def setUp(self):
    super().setUp()

    self.auth_entity = FakeToken.auth_entity = \
      FakeAuthEntity(id='0123456789', access_token_str='towkin')
    auth_key = self.auth_entity.put()
    self.source = FakeSource(id='foo.com', features=['publish'],
                             auth_entity=auth_key)
    self.source.put()
    FakeToken.oauth_state = self.source.key.urlsafe().decode()

  def assert_response(self, status=201, token='towkin', **kwargs):
    if token:
      kwargs.setdefault('headers', {})['Authorization'] = f'Bearer {token}'

    resp = self.client.post('/micropub', **kwargs)

    body = resp.get_data(as_text=True)
    self.assertEqual(status, resp.status_code, body)
    if status == 201:
      self.assertEqual('http://fake/url', resp.headers['Location'])
    return resp

  def check_entity(self, status='complete', **kwargs):
    publish = Publish.query().get()
    self.assertEqual(self.source.key, publish.source)
    self.assertEqual(status, publish.status)
    self.assertEqual('post', publish.type)
    self.assertEqual('FakeSource post label', publish.type_label())
    if status == 'complete':
      self.assertEqual({
        'id': 'fake id',
        'url': 'http://fake/url',
        'content': 'foo bar baz',
        'granary_message': 'granary message',
        **kwargs,
      }, publish.published)
    elif status == 'deleted':
      self.assertEqual({
        'url': 'http://fake/url',
        'msg': 'delete 123',
      }, publish.published)
    return publish

  def test_query_config(self):
    resp = self.client.get('/micropub?q=config')
    self.assertEqual(200, resp.status_code)
    self.assertEqual({}, resp.json)

  def test_query_source_not_implemented(self):
    resp = self.client.get('/micropub?q=source&url=abc')
    self.assertEqual(400, resp.status_code)
    self.assertEqual('not_implemented', resp.json['error'])

  def test_create_http_put(self):
    resp = self.client.put('/micropub?h=entry&content=foo&access_token=towkin')
    self.assertEqual(405, resp.status_code)
    self.assertEqual(0, Publish.query().count())

  def test_bad_content_type(self):
    resp = self.assert_response(status=400, data='foo', content_type='text/plain')
    self.assertEqual({
      'error': 'invalid_request',
      'error_description': 'Unsupported Content-Type text/plain',
    }, resp.json)
    self.assertEqual(0, Publish.query().count())

  def test_no_token(self):
    self.assert_response(status=401, token=None)
    self.assertEqual(0, Publish.query().count())

  def test_invalid_token(self):
    self.assert_response(status=401, token='bad', data={'x': 'y'})
    self.assert_response(status=401, token=None, data={'x': 'y'},
                         headers={'Authorization': 'foo bar'})
    self.assertEqual(0, Publish.query().count())

  def test_publish_not_enabled(self):
    self.source.features = ['listen']
    self.source.put()
    self.assert_response(status=403, data={
      'h': 'entry',
      'content': 'foo bar baz',
    })
    self.assertEqual(0, Publish.query().count())

  def test_unsupported_action(self):
    self.assert_response(status=400, data={'action': 'update'})
    self.assertEqual(0, Publish.query().count())

  def test_token_query_param(self):
    self.assert_response(token=None, data={
      'h': 'entry',
      'content': 'foo bar baz',
      'access_token': 'towkin',
    })
    self.check_entity()

  def test_create_form_encoded(self):
    self.assert_response(data={
      'h': 'entry',
      'content': 'foo bar baz',
    })
    self.check_entity()

  def test_create_json(self):
    self.assert_response(json={
      'type': ['h-entry'],
      'properties': {
        'content': ['foo bar baz'],
      },
    })
    self.check_entity()

  def test_create_silo_error(self):
    self.assert_response(data={
      'h': 'entry',
      'content': 'foo bar baz',
    })
    self.check_entity()

  def test_create_json_html_content(self):
    self.assert_response(json={
      'type': ['h-entry'],
      'properties': {
        'content': [{
          'html': """\
foo
<em>bar</em>
<div class="xyz"><p>baz</p></div>
""",
        }],
      },
    })
    self.check_entity(content='foo _bar_\n\nbaz')

  def test_create_form_encoded_single_photo_url(self):
    self.assert_response(data={
      'h': 'entry',
      'content': 'foo bar baz',
      'photo': 'http://img',
    })
    self.check_entity(images=['http://img'])

  def test_create_json_single_photo_url(self):
    self.assert_response(json={
      'type': ['h-entry'],
      'properties': {
        'content': ['foo bar baz'],
        'photo': ['http://img'],
      },
    })
    self.check_entity(images=['http://img'])

  def test_create_form_encoded_multiple_photo_urls(self):
    self.assert_response(data=MultiDict((
      ('h', 'entry'),
      ('content', 'foo bar baz'),
      ('photo[]', 'http://img'),
      ('photo[]', 'http://other'),
    )))
    self.check_entity(images=['http://img', 'http://other'])

  def test_create_json_multiple_photo_urls(self):
    self.assert_response(json={
      'type': ['h-entry'],
      'properties': {
        'content': ['foo bar baz'],
        'photo': ['http://img', 'http://other'],
      }
    })
    self.check_entity(images=['http://img', 'http://other'])

  def test_delete_form_encoded(self):
    self.assert_response(data={
      'action': 'delete',
      'url': 'http://fa.ke/123',
    }, status=200)
    self.check_entity(status='deleted')

  def test_delete_json(self):
    self.assert_response(json={
      'action': 'delete',
      'url': 'http://fa.ke/123',
    }, status=200)
    self.check_entity(status='deleted')

  def test_delete_no_url(self):
    self.assert_response(status=400, data={'action': 'delete'})
    self.assertEqual(0, Publish.query().count())

  def test_delete_not_silo_url(self):
    self.assert_response(status=400, data={
      'action': 'delete',
      'url': 'https://other/123',
    })
    self.assertEqual(0, Publish.query().count())

  def test_delete_no_post_id_in_url(self):
    self.assert_response(status=400, data={
      'action': 'delete',
      'url': 'https://fa.ke',
    })
    self.assertEqual(0, Publish.query().count())

  def test_get_token(self):
    resp = self.client.get('/micropub-token/fake/finish', data={
      'source_key': self.source.key.urlsafe().decode(),
    })
    self.assertEqual(302, resp.status_code)
    self.assertEqual('http://localhost/fake/foo.com', resp.headers['Location'])
    self.assertEqual(
      ['Your <a href="/about#micropub">Micropub token</a> for foo.com (FakeSource) is: <code>towkin</code>'],
      get_flashed_messages())

  def test_get_token_wrong_user(self):
    other_source = FakeSource(id='other').put()
    FakeToken.oauth_state = other_source.urlsafe().decode()

    resp = self.client.get('/micropub-token/fake/finish', data={
      'source_key': self.source.key.urlsafe().decode(),
    })
    self.assertEqual(302, resp.status_code)
    self.assertEqual('http://localhost/fake/other', resp.headers['Location'])
    self.assertEqual(
      ['To get a Micropub token for other, please log into FakeSource as that account.'],
      get_flashed_messages())

  def test_get_token_oauth_decline(self):
    FakeToken.auth_entity = None
    resp = self.client.get('/micropub-token/fake/finish', data={
      'source_key': self.source.key.urlsafe().decode(),
    })
    self.assertEqual(302, resp.status_code)
    self.assertEqual('http://localhost/fake/foo.com', resp.headers['Location'])
    self.assertEqual(
      ['If you want a Micropub token, please approve the prompt.'],
      get_flashed_messages())
