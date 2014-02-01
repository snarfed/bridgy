"""Twitter Streaming API client for handling Twitter responses in realtime.

Tweets (including retweets and replies) are reported as individual, top-level
tweet objects.
https://dev.twitter.com/docs/platform-objects/tweets

Favorites are reported via 'favorite' events:
https://dev.twitter.com/docs/streaming-apis/messages#Events_event

The Twitter Streaming API uses long-lived HTTP requests, so this is an App
Engine backend instead of a normal request handler.
https://developers.google.com/appengine/docs/python/backends/

It also (automatically) uses App Engine's Sockets API:
https://developers.google.com/appengine/docs/python/sockets/

An alternative to Twitter's Streaming API would be to scrape the HTML, e.g.:
https://twitter.com/i/activity/favorited_popup?id=415371781264781312

I originally used the requests library, which is great, but tweepy handles more
logic that's specific to Twitter's Streaming API, e.g. backoff for HTTP 420 rate
limiting.

Also, SSL with App Engine's sockets API isn't fully supported in dev_appserver.
urllib3.util.ssl_wrap_socket() makes ssl.py raise 'TypeError: must be
_socket.socket, not socket'. To work around that:

* add '_ssl' and '_socket' to _WHITE_LIST_C_MODULES in
  SDK/google/appengine/tools/devappserver2/python/sandbox.py
* replace SDK/google/appengine/dist27/socket.py with /usr/lib/python2.7/socket.py

Background:
http://stackoverflow.com/a/16937668/186123
http://code.google.com/p/googleappengine/issues/detail?id=9246
https://developers.google.com/appengine/docs/python/sockets/ssl_support
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

# i originally set this in the env_variables section in app.yaml, but that makes
# it apply to the frontend too, and i only want it to apply to this backend.
import os
os.environ['GAE_USE_SOCKETS_HTTPLIB'] = 'true'

import json
import logging
import threading
import time

from activitystreams.oauth_dropins import twitter as oauth_twitter
import appengine_config
import models
import tasks
from tweepy import streaming
from twitter import Twitter
import util

from google.appengine.api import background_thread
from google.appengine.api import runtime
import webapp2


USER_STREAM_URL = 'https://userstream.twitter.com/1.1/user.json?with=user'
# How often to check for new/deleted sources, in seconds.
UPDATE_STREAMS_PERIOD_S = 1 * 60

# globals
streams = {}  # maps Twitter key to tweepy.streaming.Stream
streams_lock = threading.Lock()
update_thread = None  # initialized in Start


class Listener(streaming.StreamListener):
  """A per-user streaming API connection.

  I'd love to use non-blocking I/O on the HTTP connections instead of thread per
  connection, but tweepy's API is at a way higher level: its only options are
  blocking or threads. Same with requests, and I think even httplib. Ah well.
  It'll be fine as long as brid.gy doesn't have a ton of users.
  """

  def __init__(self, source):
    """Args: source: Twitter
    """
    super(Listener, self).__init__()
    self.source = source

  def on_connect(self):
    logging.info('Connected! (%s)', self.source.key.string_id())

  def on_data(self, raw_data):
    try:
      # logging.debug('Received streaming message: %s...', raw_data[:100])
      data = json.loads(raw_data)

      if data.get('event') == 'favorite':
        response = self.source.as_source.streaming_event_to_object(data)
        if not response:
          logging.warning('Discarding malformed favorite event: %s', raw_data)
          return True
        tweet = data.get('target_object')
        activity = self.source.as_source.tweet_to_activity(tweet)

      elif (data.get('retweeted_status', {}).get('user', {}).get('screen_name') ==
            self.source.key.string_id()):
        response = self.source.as_source.retweet_to_object(data)
        activity = self.source.as_source.tweet_to_activity(data['retweeted_status'])

      # not handling replies right now. i wish i could, but we only get the
      # individual tweet that it's replying too, not the original root tweet that
      # started the chain, which is the one that will have the original post
      # links and should be used as the activity.
      #
      # worse, we store this as a response, even though it has the wrong
      # activity, and so when the poll task later finds it and has the correct
      # root tweet, it sees that the response has already been saved to the
      # datastore and propagated, so it drops the good one on the floor.
      #
      # sigh. oh well.
      #
      # elif ('in_reply_to_status_id_str' in data and
      #       data.get('in_reply_to_screen_name') == self.source.key.string_id()):
      #   response = self.source.as_source.tweet_to_object(data)
      #   activity = self.source.as_source.get_activities(
      #     activity_id=data['in_reply_to_status_id_str'])[0]

      else:
        # logging.debug("Discarding message we don't handle: %s", data)
        return True

      targets = tasks.get_webmention_targets(activity)
      models.Response(id=response['id'],
                      source=self.source,
                      activity_json=json.dumps(activity),
                      response_json=json.dumps(response),
                      unsent=list(targets),
                      ).get_or_save()
    except:
      logging.warning('Error processing message: %s', raw_data, exc_info=True)

    return True


def update_streams():
  """Thread function that wakes up periodically and updates stream connections.

  Connects new Twitter accounts, disconnects disabled and deleted ones,
  sleeps for a while, and repeats.
  """
  global streams_lock, update_thread

  while True:
    with streams_lock:
      try:
        update_streams_once()
      except ShutdownException:
        logging.info('Stopping update thread.')
        update_thread = None
        return
      except:
        logging.exception('Error updating streams')
    time.sleep(UPDATE_STREAMS_PERIOD_S)


def update_streams_once():
  """Connects new Twitter accounts and disconnects disabled and deleted ones.

  Separated from update_streams() mainly for testing.
  """
  global streams

  # Delete closed streams. They'll be reconnected below.
  for key, stream in streams.items():
    if not stream.running:
      del streams[key]

  query = Twitter.query(Twitter.status != 'disabled')
  sources = {t.key: t for t in query.iter()}
  stream_keys = set(streams.keys())
  source_keys = set(sources.keys())

  # Connect to new accounts
  to_connect = source_keys - stream_keys
  logging.info('Connecting %d streams', len(to_connect))
  for key in to_connect:
    source = sources[key]
    auth = oauth_twitter.TwitterAuth.tweepy_auth(
      *source.auth_entity.get().access_token())
    streams[key] = streaming.Stream(auth, Listener(source))
    # run stream in *non*-background thread, since app engine backends have a
    # fixed limit of 10 background threads per instance. normal threads are only
    # limited by memory, and since we're starting them from a background thread,
    # they're not bound to an HTTP request.
    # http://stackoverflow.com/a/20896720/186123
    streams[key].userstream(async=True)

  # Disconnect from deleted or disabled accounts
  to_disconnect = stream_keys - source_keys
  logging.info('Disconnecting %d streams', len(to_disconnect))
  for key in to_disconnect:
    streams[key].disconnect()
    del streams[key]


class Start(webapp2.RequestHandler):
  def get(self):
    runtime.set_shutdown_hook(shutdown_hook)

    global streams, streams_lock, update_thread
    with streams_lock:
      streams = {}
      if update_thread is None:
        update_thread = background_thread.start_new_background_thread(
          update_streams, [])


def shutdown_hook():
  """Runtime shutdown hook. Exceptions raised here are re-raised in all threads.

  https://developers.google.com/appengine/docs/python/backends/#Python_Shutdown
  https://developers.google.com/appengine/docs/python/backends/runtimeapi
  """
  logging.info('Shutting down!')

  global streams, streams_lock
  with streams_lock:
    for key, stream in streams.items():
      logging.info('Disconnecting %s %s', key.string_id(), key)
      stream.disconnect()
    streams = None

  raise ShutdownException()


class ShutdownException(Exception):
  """Signals that the backend is shutting down."""
  pass


application = webapp2.WSGIApplication([
    ('/_ah/start', Start),
    ], debug=appengine_config.DEBUG)
