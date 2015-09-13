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

  def test_new(self):
    inst = Instagram.new(self.handler, auth_entity=self.auth_entity)
    self.assertEqual(self.auth_entity, inst.auth_entity.get())
    self.assertEqual('my_token', inst.gr_source.access_token)
    self.assertEqual('snarfed', inst.key.string_id())
    self.assertEqual('http://pic.ture/url', inst.picture)
    self.assertEqual('http://instagram.com/snarfed', inst.url)
    self.assertEqual('http://instagram.com/snarfed', inst.silo_url())
    self.assertEqual('tag:instagram.com,2013:my_string_id', inst.user_tag_id())
    self.assertEqual('Ryan Barrett', inst.name)

  def test_get_activities_response(self):
    """Check that min_id is discarded."""
    inst = Instagram.new(self.handler, auth_entity=self.auth_entity)
    self.expect_urlopen(
      'https://api.instagram.com/v1/users/self/media/recent?access_token=my_token',
      '{"data":[]}')
    self.mox.ReplayAll()
    assert inst.get_activities_response(min_id='123')

  def test_canonicalize_syndication_url(self):
    inst = Instagram.new(self.handler, auth_entity=self.auth_entity)

    for url in (
        'http://www.instagram.com/p/abcd',
        'https://www.instagram.com/p/abcd',
        'https://instagram.com/p/abcd',
    ):
      self.assertEqual(
        'http://instagram.com/p/abcd',
        inst.canonicalize_syndication_url(url))
