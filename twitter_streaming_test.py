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

from google.appengine.ext import db


class TwitterStreamingTest(testutil.ModelsTest):

  def setUp(self):
    super(TwitterStreamingTest, self).setUp()
    self.source = self.make_source('unused')
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

    # expect connects and disconnects
    new_streams = [self.mox.CreateMock(streaming.Stream) for i in range(2)]
    self.mox.StubOutWithMock(streaming, 'Stream')
    for name, new_stream in (('stopped', self.mox.CreateMock(streaming.Stream)),
                             ('new', self.mox.CreateMock(streaming.Stream))):
      streaming.Stream(mox.IgnoreArg(), mox.IgnoreArg()).AndReturn(new_stream)
      new_stream.userstream(async=True)
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

  # right now, to test this, uncomment it and check that the test hangs instead
  # of exiting. TODO: do better. :P
  # def test_update_streams_once_exception(self):
  #   self.mox.StubOutWithMock(twitter_streaming, 'update_streams_once')
  #   twitter_streaming.update_streams_once().AndRaise(Exception('foo'))
  #   self.mox.ReplayAll()
  #   twitter_streaming.update_streams()
