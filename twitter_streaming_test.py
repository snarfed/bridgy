"""Unit tests for twitter_streaming.py.
"""

import copy
import json

from activitystreams import twitter_test
from activitystreams.oauth_dropins import twitter as oauth_twitter
import models
import testutil
import twitter
import twitter_streaming

from google.appengine.ext import db


class TwitterStreamingTest(testutil.ModelsTest):

  def test_favorite_listener(self):
    auth_entity = oauth_twitter.TwitterAuth(
      key_name='unused', auth_code='my_code', token_key='my_key',
      token_secret='my_secret', user_json='{}')
    source = twitter.Twitter(key_name='unused', auth_entity=auth_entity)
    listener = twitter_streaming.FavoriteListener(source)

    # not a favorite
    self.assertTrue(listener.on_data(json.dumps({'event': 'foo'})))
    self.assertEqual(0, models.Response.all().count())

    # missing data
    self.assertTrue(listener.on_data(json.dumps({'event': 'favorite'})))
    self.assertEqual(0, models.Response.all().count())

    # exception
    self.assertTrue(listener.on_data('not json'))

    # valid
    self.assertTrue(listener.on_data(json.dumps(twitter_test.FAVORITE_EVENT)))
    self.assertEqual(1, models.Response.all().count())
    resp = models.Response.all().get()
    self.assertEqual(twitter_test.LIKE['id'], resp.key().name())
    self.assert_equals(twitter_test.LIKE, json.loads(resp.response_json))

    activity = copy.deepcopy(twitter_test.ACTIVITY)
    activity['object']['tags'].append(
      {'objectType': 'article', 'url': 'http://t.co/6J2EgYM'})
    self.assert_equals(activity, json.loads(resp.activity_json))
