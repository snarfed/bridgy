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
    activity['object']['tags'].append(
      {'objectType': 'article', 'url': 'http://t.co/6J2EgYM'})
    self.assert_equals(activity, json.loads(resp.activity_json))

  def test_update_streams_stopped(self):
    twitter_streaming.streams = None
    self.mox.ReplayAll()
    twitter_streaming.update_streams_once()  # shouldn't start any threads
    self.assertIsNone(twitter_streaming.streams)

  def test_update_streams(self):
    sources = {name: self.make_source(name)
               for name in ('existing', 'new', 'disabled', 'error', 'deleted')}
    sources['disabled'].status = 'disabled'
    sources['error'].status = 'error'
    for source in sources.values():
      source.save()

    disabled_stream = self.mox.CreateMock(streaming.Stream)
    deleted_stream = self.mox.CreateMock(streaming.Stream)
    twitter_streaming.streams = {
      sources['existing'].key(): None,  # these shouldn't be touched
      sources['error'].key(): None,
      sources['disabled'].key(): disabled_stream,
      sources['deleted'].key(): deleted_stream,
      }

    # expect connect and disconnects
    def is_new_source(stream_method):
      stream = stream_method.__self__
      self.assertEquals('new', stream.listener.source.key().name())
      self.assertEquals('new key', stream.auth.access_token.key)
      self.assertEquals('new secret', stream.auth.access_token.secret)
      return True

    background_thread.start_new_background_thread(mox.Func(is_new_source), [])
    disabled_stream.disconnect()
    deleted_stream.disconnect()
    self.mox.ReplayAll()

    sources['deleted'].delete()
    twitter_streaming.update_streams_once()

    self.assert_equals([sources['existing'].key(),
                        sources['error'].key(),
                        sources['new'].key()],
                       twitter_streaming.streams.keys())

