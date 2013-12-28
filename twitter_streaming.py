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

I use tweepy to access the Streaming API. I originally used the requests
library, which worked great standalone, but isn't fully supported on App Engine.
I hit this error:

  File "/Users/ryan/src/bridgy/activitystreams/oauth_dropins/requests/packages/urllib3/util.py", line 643, in ssl_wrap_socket
    ssl_version=ssl_version)
  File "/usr/lib/python2.7/ssl.py", line 387, in wrap_socket
    ciphers=ciphers)
  File "/usr/lib/python2.7/ssl.py", line 141, in __init__
    ciphers)
  TypeError: must be _socket.socket, not socket
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import json
import logging
import threading
import time

from activitystreams.oauth_dropins import twitter as oauth_twitter
import appengine_config
import requests
import tasks
import twitter
import util

import webapp2

USER_STREAM_URL = 'https://userstream.twitter.com/1.1/user.json?with=user'
# How often to check for new/deleted sources, in seconds.
POLL_FREQUENCY_S = 5 * 60


class Stream(threading.Thread):
  """A streaming API connection for a single user.

  I'd love to use non-blocking I/O on the HTTP connections instead of thread per
  connection, but requests doesn't support it, and httplib's support is weak
  (and its API is low level and pretty painful to use.) So, I went with thread
  per connection. It'll be fine as long as brid.gy doesn't have a ton of users.
  """

  # twitter.Twitter. set in Start.get().
  source = None

  def run(self):
    """The thread target.
    """
    self.stopped = threading.Event()
    logging.info('Connecting to %s %s', self.source.key().name(),
                 self.source.key())
    token = self.source.auth_entity.access_token()
    headers = oauth_twitter.TwitterAuth.auth_header(USER_STREAM_URL, *token)

    self.conn = requests.get(USER_STREAM_URL, headers=headers, stream=True)
    if self.conn.status_code == 200:
      logging.info('Connected! %s', self.conn)
    else:
      logging.error("Couldn't connect: %s", self.conn)
      self.conn.close()
      return

    for line in self.conn.iter_items():
      logging.info('Streaming: %s', line)
      if not line:
        continue  # discard keep-alive blank lines

      try:
        event = json.loads(line)
        like = self.source.streaming_event_to_object(event)
        if data.get('event') != 'favorite' or not like:
          logging.debug('Discarding non-favorite message: %s', line)
          continue

        tweet = json.loads(self.source.get('target_object'))
        activity = self.source.tweet_to_activity(tweet)
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
        logging.exception('Error processing message: %s', line)

  def stop(self):
    logging.info('Disconnecting from %s', self.name)
    self.stopped


class Start(webapp2.RequestHandler):
  def get(self):
    streams = {}  # maps Twitter key to Stream

    while True:
      query = twitter.Twitter.all().filter('status !=', 'disabled')
      sources = {t.key(): t for t in query}
      stream_keys = set(streams.keys())
      source_keys = set(sources.keys())

      # Connect to new accounts
      for key in source_keys - stream_keys:
        streams[key] = Stream(name=key.name())
        streams[key].source = sources[key]
        streams[key].start()

      # Disconnect from deleted or disabled accounts
      # TODO: if access is revoked on Twitter's end, the request will disconnect.
      # handle that.
      # https://dev.twitter.com/docs/streaming-apis/messages#Events_event
      # for key in source_keys - stream_keys:
      #   streams[key].stop()
      #   del streams[key]

      time.sleep(POLL_FREQUENCY_S)


class Stop(webapp2.RequestHandler):
  def get(self):
    logging.info('Stopping.')
    # TODO


application = webapp2.WSGIApplication([
    ('/_ah/start', Start),
    ('/_ah/stop', Stop),
    ], debug=appengine_config.DEBUG)
