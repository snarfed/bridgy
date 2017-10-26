"""Handler that exposes app logs to users."""

import webapp2

import appengine_config
from oauth_dropins.webutil import logs


class LogHandler(logs.LogHandler):
  VERSION_IDS = ['2', '3', '4', '5', '6', '7']


application = webapp2.WSGIApplication([
    ('/log', LogHandler),
], debug=appengine_config.DEBUG)
