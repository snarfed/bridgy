"""Unit tests for twitter_streaming.py.
"""

import copy
import json
import mox

from activitystreams import twitter_test
from activitystreams.oauth_dropins import twitter as oauth_twitter
import models
import testutil
from tweepy import streaming
import twitter
import twitter_streaming

from google.appengine.api import background_thread
from google.appengine.ext import db


class TwitterStreamingTest(testutil.ModelsTest):

  def setUp(self):
    super(TwitterStreamingTest, self).setUp()
    self.source = self.make_source('unused')
    self.mox.StubOutWithMock(background_thread, 'start_new_background_thread')
    twitter_streaming.streams = {}

  def make_source(self, name):
    auth_entity = oauth_twitter.TwitterAuth(
      key_name=name, auth_code='my_code', token_key='%s key' % name,
      token_secret='%s secret' % name, user_json='{}')
    auth_entity.save()
    return twitter.Twitter(key_name=name, auth_entity=auth_entity)

  def test_favorite_listener(self):
    source = self.make_source('name')
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
    self.assert_equals(activity, json.loads(resp.activity_json))
    self.assert_equals(['http://first/link/'], resp.unsent)

  def test_update_streams_stopped(self):
    twitter_streaming.streams = None
    self.mox.ReplayAll()
    twitter_streaming.update_streams_once()  # shouldn't start any threads
    self.assertIsNone(twitter_streaming.streams)

  def test_update_streams(self):
    sources = {name: self.make_source(name) for name in
               ('existing', 'new', 'disabled', 'error', 'deleted', 'stopped')}
    sources['disabled'].status = 'disabled'
    sources['error'].status = 'error'
    for source in sources.values():
      source.save()

    for name in 'existing', 'error', 'disabled', 'deleted', 'stopped':
      stream = self.mox.CreateMock(streaming.Stream)
      stream.running = (name != 'stopped')
      twitter_streaming.streams[sources[name].key()] = stream

    # expect connect and disconnects
    def is_source(name):
      def check(stream_method):
        stream = stream_method.__self__
        self.assertEquals(name, stream.listener.source.key().name())
        self.assertEquals(name + ' key', stream.auth.access_token.key)
        self.assertEquals(name + ' secret', stream.auth.access_token.secret)
        return True
      return check

    background_thread.start_new_background_thread(
      mox.Func(is_source('stopped')), [])
    background_thread.start_new_background_thread(
      mox.Func(is_source('new')), [])
    for name in 'disabled', 'deleted':
      twitter_streaming.streams[sources[name].key()].disconnect()

    self.mox.ReplayAll()

    sources['deleted'].delete()
    twitter_streaming.update_streams_once()

    self.assert_equals([sources['existing'].key(),
                        sources['error'].key(),
                        sources['new'].key(),
                        sources['stopped'].key(),
                        ],
                       twitter_streaming.streams.keys())

