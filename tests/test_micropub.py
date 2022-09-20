"""Unit tests for micropub.py."""
import html
from io import BytesIO

import micropub
from models import Publish, PublishedPage
from .testutil import AppTest, FakeAuthEntity, FakeSource


class MicropubTest(AppTest):

  @classmethod
  def setUpClass(cls):
    micropub.SOURCE_CLASSES = (
      (FakeSource, FakeAuthEntity, FakeAuthEntity.access_token),
    ) + micropub.SOURCE_CLASSES

  def setUp(self):
    super().setUp()

    self.auth_entity = FakeAuthEntity(id='0123456789', access_token='towkin')
    auth_key = self.auth_entity.put()
    self.source = FakeSource(id='foo.com', features=['publish'],
                             auth_entity=auth_key)
    self.source.put()

  def assert_response(self, status=201, token='towkin', **kwargs):
    if token:
      kwargs.setdefault('headers', {})['Authorization'] = f'Bearer {token}'

    resp = self.client.post('/micropub', **kwargs)

    body = resp.get_data(as_text=True)
    self.assertEqual(status, resp.status_code, body)
    if status // 100 == 2:
      self.assertEqual('http://fake/url', resp.headers['Location'])
    return resp

  def check_entity(self, url='http://foo', **kwargs):
    self.assertTrue(PublishedPage.get_by_id(url))
    publish = Publish.query().get()
    self.assertEqual(self.source.key, publish.source)
    self.assertEqual('complete', publish.status)
    self.assertEqual('post', publish.type)
    self.assertEqual('FakeSource post label', publish.type_label())
    self.assertEqual({
      'id': 'fake id',
      'url': 'http://fake/url',
      'content': 'foo bar baz',
      'granary_message': 'granary message',
    }, publish.published)

  def test_query_config(self):
    resp = self.client.get('/micropub?q=config')
    self.assertEqual(200, resp.status_code)
    self.assertEqual({}, resp.json)

  def test_query_source_not_implemented(self):
    resp = self.client.get('/micropub?q=source&url=abc')
    self.assertEqual(400, resp.status_code)
    self.assertEqual({'error': 'not_implemented'}, resp.json)

  def test_bad_content_type(self):
    resp = self.assert_response(status=400, data='foo', content_type='text/plain')
    self.assertEqual({
      'error': 'invalid_request',
      'error_description': 'Unsupported Content-Type text/plain',
    }, resp.json)

  def test_no_token(self):
    self.assert_response(status=401, token=None)

  def test_invalid_token(self):
    self.assert_response(status=401, token='bad', data={'x': 'y'})
    self.assert_response(status=401, token=None, data={'x': 'y'},
                         headers={'Authorization': 'foo bar'})

  def test_token_query_param(self):
    self.assert_response(data={
      'url': 'http://foo',
      'h': 'entry',
      'content': 'foo bar baz',
      'access_token': 'towkin',
    })

  def test_already_published(self):
    page = PublishedPage(id='http://foo')
    Publish(parent=page.key, source=self.source.key, status='complete',
            type='post', published={'content': 'foo'}).put()

    self.assert_response(status=400, data={
      'url': 'http://foo',
      'h': 'entry',
      'content': 'foo bar baz',
    })

  def test_create_form_encoded(self):
    resp = self.assert_response(data={
      'url': 'http://foo',
      'h': 'entry',
      'content': 'foo bar baz',
    })
    self.check_entity(content='foo bar baz')

  # def test_create_form_encoded_token_param(self):
  #   resp = self.client.post('/micropub', data={
  #     'h': 'entry',
  #     'content': 'Micropub+test+of+creating+a+basic+h-entry',
  #   })
  #   body = html.unescape(resp.get_data(as_text=True))
  #   self.assertEqual(201, resp.status_code,
  #                    f'201 != {resp.status_code}: {body}')
  #   self.assertEqual('xyz', resp.headers['Location'])

  # def test_create_form_encoded_multiple_categories(self):
  #   resp = self.assert_response(data={
  #     'url': 'http://foo',
  #     'h': 'entry',
  #     'content': 'foo bar baz',
  #     'category[]': 'A',
  #     'category[]': 'B',
  #   })
  #   self.check_entity(content='foo bar baz')

#   def test_create_form_encoded_photo_url(self):
# Content-type: application/x-www-form-urlencoded; charset=utf-8

# h=entry
# content=Micropub+test+of+creating+a+photo+referenced+by+URL
# photo=http://TODO

#   def test_create_form_encoded_reply(self):
# Content-type: application/x-www-form-urlencoded; charset=utf-8

# h=entry
# content=Micropub+test+of+creating+an+h-entry+with+one+category.+This+post+should+have+one+category,+test1
# category=test1

#   def test_create_form_encoded_like(self):
# Content-type: application/x-www-form-urlencoded; charset=utf-8

# h=entry
# content=Micropub+test+of+creating+an+h-entry+with+one+category.+This+post+should+have+one+category,+test1
# category=test1

#   def test_create_form_encoded_repost(self):
# Content-type: application/x-www-form-urlencoded; charset=utf-8

# h=entry
# content=Micropub+test+of+creating+an+h-entry+with+one+category.+This+post+should+have+one+category,+test1
# category=test1

  def test_create_json(self):
    resp = self.assert_response(json={
      'type': ['h-entry'],
      'properties': {
        'content': ['foo bar baz'],
        'url': ['http://foo'],
      },
    })
    self.check_entity()

#   def test_create_json_multiple_categories(self):
# {
#   "type": ["h-entry"],
#   "properties": {
#     "content": ["Micropub test of creating an h-entry with a JSON request containing multiple categories. This post should have two categories, test1 and test2."],
#     "category": [
#       "test1",
#       "test2"
#     ]
#   }
# }

#   def test_create_json_html_content(self):
# {
#   "type": ["h-entry"],
#   "properties": {
#     "content": [{
#       "html": "TODO"
#     }]
#   }
# }

#   def test_create_json_photo_url(self):
# {
#   "type": ["h-entry"],
#   "properties": {
#     "content": ["Micropub test of creating a photo referenced by URL. This post should include a photo of a sunset."],
#     "photo": ["media/sunset.jpg"]
#   }
# }

#   def test_create_json_nested_checkin(self):
# {
#     "type": [
#         "h-entry"
#     ],
#     "properties": {
#         "published": [
#             "2017-05-31T12:03:36-07:00"
#         ],
#         "content": [
#             "Lunch meeting"
#         ],
#         "checkin": [
#             {
#                 "type": [
#                     "h-card"
#                 ],
#                 "properties": {
#                     "name": ["Los Gorditos"],
#                     "url": ["https://foursquare.com/v/502c4bbde4b06e61e06d1ebf"],
#                     "latitude": [45.524330801154],
#                     "longitude": [-122.68068808051],
#                     "street-address": ["922 NW Davis St"],
#                     "locality": ["Portland"],
#                     "region": ["OR"],
#                     "country-name": ["United States"],
#                     "postal-code": ["97209"]
#                 }
#             }
#         ]
#     }
# }

#   def test_create_json_multiple_photo_urls(self):
# {
#   "type": ["h-entry"],
#   "properties": {
#     "content": ["Micropub test of creating multiple photos referenced by URL. This post should include a photo of a city at night."],
#     "photo": [
#       "media/sunset.jpg",
#       "media/city-at-night.jpg"
#     ]
#   }
# }

#   def test_create_json_photo_alt_text(self):
# {
#   "type": ["h-entry"],
#   "properties": {
#     "content": ["Micropub test of creating a photo referenced by URL with alt text. This post should include a photo of a sunset."],
#     "photo": [
#       {
#         "value": "media/sunset.jpg",
#         "alt": "Photo of a sunset"
#       }
#     ]
#   }
# }

  # def test_create_multipart_photo(self):
  #   resp = self.assert_response(data={
  #     'url': 'http://foo',
  #     'h': 'entry',
  #     'photo': (BytesIO('photo contents'), 'filename'),
  #   })
  #   self.check_entity()

#   def test_create_multipart_multiple_photos(self):
# multipart/form-data; boundary=553d9cee2030456a81931fb708ece92c

# --553d9cee2030456a81931fb708ece92c
# Content-Disposition: form-data; name="h"

# entry
# --553d9cee2030456a81931fb708ece92c
# Content-Disposition: form-data; name="content"

# Hello World!
# --553d9cee2030456a81931fb708ece92c
# Content-Disposition: form-data; name="photo"; filename="aaronpk.png"
# Content-Type: image/png
# Content-Transfer-Encoding: binary

# ... (binary data) ...
# --553d9cee2030456a81931fb708ece92c--

#   def test_create_multipart_multiple_categories(self):
# multipart/form-data; boundary=553d9cee2030456a81931fb708ece92c

# --553d9cee2030456a81931fb708ece92c
# Content-Disposition: form-data; name="h"

# entry
# --553d9cee2030456a81931fb708ece92c
# Content-Disposition: form-data; name="content"

# Hello World!
# --553d9cee2030456a81931fb708ece92c
# Content-Disposition: form-data; name="category[]"

# foo
# --553d9cee2030456a81931fb708ece92c
# Content-Disposition: form-data; name="category[]"

# bar
# --553d9cee2030456a81931fb708ece92c--

#   def test_delete_form_encoded():
#   'postbody' => 'h=entrycontent=This+post+will+be+deleted+when+the+test+succeeds.',
#   'content_type' => 'form',
#   'deletebody' => 'action=deleteurl=%%%',

#   def test_undelete_form_encoded():
#   'postbody' => 'h=entry&amp;content=This+post+will+be+deleted,+and+should+be+restored+after+undeleting+it.',
#   'content_type' => 'form',
#   'deletebody' => 'action=delete&amp;url=%%%',
#   'undeletebody' => 'action=undelete&amp;url=%%%',
#     400

#   def test_delete_json():
#   'postbody' => '{"type":["h-entry"],"properties":{"content":["This post will be deleted when the test succeeds."]}}',
#   'content_type' => 'json',
#   'deletebody' => '{"action":"delete","url":"%%%"}',

#   def test_undelete_json():
#   'postbody' => '{"type":["h-entry"],"properties":{"content":["This post will be deleted, and should be restored after undeleting it."]}}',
#   'content_type' => 'json',
#   'deletebody' => '{"action":"delete","url":"%%%"}',
#   'undeletebody' => '{"action":"undelete","url":"%%%"}',
#     400
