"""Unit tests for instagram.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import json


from oauth_dropins import instagram as oauth_instagram

from instagram import Instagram
import testutil


class InstagramTest(testutil.ModelsTest):

  def setUp(self):
    super(InstagramTest, self).setUp()
    self.handler.messages = []
    self.auth_entity = oauth_instagram.InstagramAuth(
      id='my_string_id', auth_code='my_code', access_token_str='my_token',
      user_json=json.dumps({'username': 'snarfed',
                            'full_name': 'Ryan Barrett',
                            'bio': 'something about me',
                            'profile_picture': 'http://pic.ture/url',
                            'id': 'my_string_id',
                            }))
    self.auth_entity.put()
    self.inst = Instagram.new(self.handler, auth_entity=self.auth_entity)

  def test_new(self):
    self.assertEqual(self.auth_entity, self.inst.auth_entity.get())
    self.assertEqual('my_token', self.inst.gr_source.access_token)
    self.assertEqual('snarfed', self.inst.key.string_id())
    self.assertEqual('http://pic.ture/url', self.inst.picture)
    self.assertEqual('http://instagram.com/snarfed', self.inst.url)
    self.assertEqual('http://instagram.com/snarfed', self.inst.silo_url())
    self.assertEqual('tag:instagram.com,2013:my_string_id', self.inst.user_tag_id())
    self.assertEqual('Ryan Barrett', self.inst.name)
    self.assertEqual('snarfed (Instagram)', self.inst.label())

  def test_canonicalize_url(self):
    self.unstub_requests_head()
    for url in (
        'http://www.instagram.com/p/abcd',
        'https://www.instagram.com/p/abcd',
        'https://www.instagram.com/p/abcd/',
        'https://instagram.com/p/abcd',
    ):
      self.assertEqual('https://www.instagram.com/p/abcd/',
                       self.inst.canonicalize_url(url))

    self.assertIsNone(self.inst.canonicalize_url('https://www.foo.com/p/abcd/'))
