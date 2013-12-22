"""Unit tests for twitter.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import json
import testutil

from activitystreams import twitter_test as as_twitter_test
from activitystreams.oauth_dropins import twitter as oauth_twitter
from twitter import Twitter


class TwitterTest(testutil.ModelsTest):

  def setUp(self):
    super(TwitterTest, self).setUp()
    self.handler.messages = []
    self.auth_entity = oauth_twitter.TwitterAuth(
      key_name='my_key_name', auth_code='my_code',
      token_key='my_key', token_secret='my_secret',
      user_json=json.dumps({'name': 'Ryan Barrett',
                            'screen_name': 'snarfed_org',
                            'description': 'something about me',
                            'profile_image_url': 'http://pi.ct/ure',
                            }))

  def test_new(self):
    tw = Twitter.new(self.handler, auth_entity=self.auth_entity)
    self.assertEqual(self.auth_entity, tw.auth_entity)
    self.assertEqual('my_key', tw.as_source.access_token_key)
    self.assertEqual('my_secret', tw.as_source.access_token_secret)
    self.assertEqual('snarfed_org', tw.key().name())
    self.assertEqual('http://pi.ct/ure', tw.picture)
    self.assertEqual('Ryan Barrett', tw.name)

  def test_get_activities(self):
    self.expect_urlopen('https://api.twitter.com/1.1/statuses/user_timeline.json?'
                        'include_entities=true&count=0',
      json.dumps([as_twitter_test.TWEET]))
    self.mox.ReplayAll()

    tw = Twitter.new(self.handler, auth_entity=self.auth_entity)
    self.assert_equals([as_twitter_test.ACTIVITY], tw.get_activities())
