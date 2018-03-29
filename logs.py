"""Handler that exposes app logs to users."""
from __future__ import unicode_literals

import webapp2

import appengine_config
from oauth_dropins.webutil import logs


class LogHandler(logs.LogHandler):
  MODULE_VERSIONS = \
    [('default', ver) for ver in '2', '3', '4', '5', '6', '7', '8'] + \
    [('background', ver) for ver in '7', '8']


application = webapp2.WSGIApplication([
    ('/log', LogHandler),
], debug=appengine_config.DEBUG)
