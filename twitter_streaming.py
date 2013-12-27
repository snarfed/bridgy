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
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import json
import logging
#import re
import urllib

#from activitystreams import twitter as as_twitter
from activitystreams.oauth_dropins import twitter as oauth_twitter
import appengine_config
#import models
import requests
import twitter
import util

import webapp2

USER_STREAM_URL = 'https://userstream.twitter.com/1.1/user.json?with=user'


class Start(webapp2.RequestHandler):
  # TODO: flush logs to generate a log per favorite event, so we can link to
  # each one from the dashboard.
  # https://developers.google.com/appengine/docs/python/backends/#Python_Periodic_logging

  reqs = {}  # maps Twitter key to Request
  while True:
    query = twitter.Twitter.all().filter('status !=', 'disabled')
    sources = {t.key(): t for t in query}
    req_keys = set(reqs.keys)
    source_keys = set(sources.keys())

    # Connect to new accounts
    for key in source_keys - req_keys:
      source = sources[key]
      logging.info('Connecting to %s %s ', source.key_name(), str(key))
      token = source.auth_entity.access_token()
      header = oauth_twitter.TwitterAuth.auth_header(USER_STREAM_URL, *token)
      reqs[key] = ...
      ...

    # Disconnect from deleted or disabled accounts
    for key in source_keys - req_keys:
      logging.info('Disconnecting from %s %s ', source.key_name(), str(key))
      reqs[key].close()
      del reqs[key]


application = webapp2.WSGIApplication([
    ('/_ah/start', Start),
    ], debug=appengine_config.DEBUG)
