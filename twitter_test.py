"""Unit tests for twitter.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import json
import testutil

from activitystreams import twitter_test as as_twitter_test
from activitystreams.oauth_dropins import twitter as oauth_twitter
import models
from twitter import Twitter


class TwitterTest(testutil.ModelsTest):

  def setUp(self):
    super(TwitterTest, self).setUp()
    self.handler.messages = []
    self.auth_entity = oauth_twitter.TwitterAuth(
      id='my_string_id',
      token_key='my_key', token_secret='my_secret',
      user_json=json.dumps({'name': 'Ryan Barrett',
                            'screen_name': 'snarfed_org',
                            'description': 'something about me',
                            'profile_image_url': 'http://pi.ct/ure',
                            }))
    self.auth_entity.put()

  def test_new(self):
    tw = Twitter.new(self.handler, auth_entity=self.auth_entity)
    self.assertEqual(self.auth_entity, tw.auth_entity.get())
    self.assertEqual('my_key', tw.as_source.access_token_key)
    self.assertEqual('my_secret', tw.as_source.access_token_secret)
    self.assertEqual('snarfed_org', tw.key.string_id())
    self.assertEqual('http://pi.ct/ure', tw.picture)
    self.assertEqual('Ryan Barrett', tw.name)

  def test_new_massages_profile_image(self):
    """We should use profile_image_url_https and drop '_normal' if possible."""
    user = json.loads(self.auth_entity.user_json)
    user['profile_image_url_https'] = 'https://foo_normal.xyz'
    self.auth_entity.user_json = json.dumps(user)

    tw = Twitter.new(self.handler, auth_entity=self.auth_entity)
    self.assertEqual('https://foo.xyz', tw.picture)

  def test_get_activities(self):
    self.expect_urlopen('https://api.twitter.com/1.1/statuses/user_timeline.json?'
                        'include_entities=true&count=0',
      json.dumps([as_twitter_test.TWEET]))
    self.mox.ReplayAll()

    tw = Twitter.new(self.handler, auth_entity=self.auth_entity)
    self.assert_equals([as_twitter_test.ACTIVITY], tw.get_activities())

  def test_get_like(self):
    """get_like() should use the Response stored in the datastore."""
    like = {
      'objectType': 'activity',
      'verb': 'like',
      'id': 'tag:twitter.com,2013:222',
      'object': {'url': 'http://my/favorite'},
      }
    models.Response(id='tag:twitter.com,2013:000_favorited_by_222',
                    response_json=json.dumps(like)).put()

    tw = Twitter.new(self.handler, auth_entity=self.auth_entity)
    self.assert_equals(like, tw.get_like('unused', '000', '222'))

