"""Twitter Streaming API client for receiving and handling favorites.

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
import twitter
import util

from google.appengine.api import background_thread
import webapp2


USER_STREAM_URL = 'https://userstream.twitter.com/1.1/user.json?with=user'
# How often to check for new/deleted sources, in seconds.
UPDATE_STREAMS_PERIOD_S = 5 * 60

# globals
streams = {}  # maps twitter.Twitter key to tweepy.streaming.Stream
streams_lock = threading.Lock()
update_thread = None  # initialized in Start

class FavoriteListener(streaming.StreamListener):
  """A per-user streaming API connection that saves favorites as Responses.

  I'd love to use non-blocking I/O on the HTTP connections instead of thread per
  connection, but tweepy's API is at a way higher level: its only options are
  blocking or threads. Same with requests, and I think even httplib. Ah well.
  It'll be fine as long as brid.gy doesn't have a ton of users.
  """

  def __init__(self, source):
    """Args: source: twitter.Twitter
    """
    super(FavoriteListener, self).__init__()
    self.source = source

  def on_connect(self):
    logging.info('Connected! (%s)', self.source.key().name())

  def on_data(self, raw_data):
    try:
      # logging.debug('Received streaming message: %s...', raw_data[:100])
      data = json.loads(raw_data)
      if data.get('event') != 'favorite':
        # logging.debug('Discarding non-favorite message: %s', raw_data)
        return True

      like = self.source.as_source.streaming_event_to_object(data)
      if not like:
        logging.debug('Discarding malformed favorite event: %s', raw_data)
        return True

      tweet = data.get('target_object')
      activity = self.source.as_source.tweet_to_activity(tweet)
      targets = tasks.get_webmention_targets(activity)
      models.Response(key_name=like['id'],
                      source=self.source,
                      activity_json=json.dumps(activity),
                      response_json=json.dumps(like),
                      unsent=list(targets),
                      ).get_or_save()
      # TODO: flush logs to generate a log per favorite event, so we can link
      # to each one from the dashboard.
      # https://developers.google.com/appengine/docs/python/backends/#Python_Periodic_logging
    except:
      logging.exception('Error processing message: %s', raw_data)

    return True


def update_streams():
  """Thread function that wakes up periodically and updates stream connections.

  Connects new Twitter accounts, disconnects disabled and deleted ones,
  sleeps for a while, and repeats.
  """
  global streams_lock

  while True:
    with streams_lock:
      update_streams_once()
    time.sleep(UPDATE_STREAMS_PERIOD_S)


def update_streams_once():
  """Connects new Twitter accounts and disconnects disabled and deleted ones.

  Separated from update_streams() mainly for testing.
  """
  global streams
  if streams is None:
    # we're currently stopped
    return

  # Delete closed streams
  for key, stream in streams.items():
    if not stream.running:
      del streams[key]

  query = twitter.Twitter.all().filter('status !=', 'disabled')
  sources = {t.key(): t for t in query}
  stream_keys = set(streams.keys())
  source_keys = set(sources.keys())

  # Connect to new accounts
  for key in source_keys - stream_keys:
    logging.info('Connecting %s %s', key.name(), key)
    source = sources[key]
    auth = oauth_twitter.TwitterAuth.tweepy_auth(
      *source.auth_entity.access_token())
    streams[key] = streaming.Stream(auth, FavoriteListener(source))
    background_thread.start_new_background_thread(streams[key].userstream, [])

  # Disconnect from deleted or disabled accounts
  for key in stream_keys - source_keys:
    streams[key].disconnect()
    del streams[key]


class Start(webapp2.RequestHandler):
  def get(self):
    global streams, streams_lock, update_thread
    with streams_lock:
      streams = {}
      if update_thread is None:
        update_thread = background_thread.start_new_background_thread(
          update_streams, [])


class Stop(webapp2.RequestHandler):
  def get(self):
    global streams, streams_lock
    with streams_lock:
      for key, stream in streams.items():
        logging.info('Disconnecting %s %s', key.name(), key)
        stream.disconnect()
      streams = None


application = webapp2.WSGIApplication([
    ('/_ah/start', Start),
    ('/_ah/stop', Stop),
    ], debug=appengine_config.DEBUG)
