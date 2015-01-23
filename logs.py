"""Handlers and utilities for exposing app logs to users.
"""

import cgi
import datetime
import re
import urllib

import appengine_config
import util

from google.appengine.api import logservice
import webapp2


LEVELS = {
  logservice.LOG_LEVEL_DEBUG:    'D',
  logservice.LOG_LEVEL_INFO:     'I',
  logservice.LOG_LEVEL_WARNING:  'W',
  logservice.LOG_LEVEL_ERROR:    'E',
  logservice.LOG_LEVEL_CRITICAL: 'F',
  }


SANITIZE_RE = re.compile(
  r"""((?:access|api|oauth)?[ _]?
       (?:consumer_key|consumer_secret|nonce|secret|signature|token|verifier)
       (?:=|:|\ |',\ u?'|%3D)\ *)
      [^ &=']+""",
  flags=re.VERBOSE | re.IGNORECASE)

def sanitize(msg):
  """Sanitizes access tokens and Authorization headers."""
  return SANITIZE_RE.sub(r'\1...', msg)


# datastore string keys are url-safe-base64 of, say, at least 40(ish) chars.
# https://cloud.google.com/appengine/docs/python/ndb/keyclass#Key_urlsafe
# http://tools.ietf.org/html/rfc3548.html#section-4
DATASTORE_KEY_RE = re.compile("'(([A-Za-z0-9-_=]{8})[A-Za-z0-9-_=]{32,})'")

def linkify_datastore_keys(msg):
  """Converts string datastore keys to links to the admin console viewer."""
  return DATASTORE_KEY_RE.sub(
    r"'<a title='\1' href='https://appengine.google.com/datastore/edit?app_id=s~brid-gy&key=\1'>\2...</a>'",
    msg)


class LogHandler(webapp2.RequestHandler):
  """Searches for and renders the app logs for a single task queue request.
  """

  def get(self):
    """URL parameters:
      start_time: float, seconds since the epoch
      key: string that should appear in the first app log
    """
    start_time = float(util.get_required_param(self, 'start_time'))
    key = urllib.unquote(util.get_required_param(self, 'key'))

    # the propagate task logs the poll task's URL, which includes the source
    # entity key as a query param. exclude that with this heuristic.
    key_re = re.compile('[^=]' + key)

    self.response.headers['Content-Type'] = 'text/html; charset=utf-8'

    offset = None
    for log in logservice.fetch(start_time=start_time, end_time=start_time + 120,
                                offset=offset, include_app_logs=True,
                                version_ids=['2', '3', '4', '5', '6', '7']):
      first_lines = '\n'.join([line.message.decode('utf-8') for line in
                               log.app_logs[:min(10, len(log.app_logs))]])
      if log.app_logs and key_re.search(first_lines):
        # found it! render and return
        self.response.out.write("""\
<html>
<body style="font-family: monospace; white-space: pre">
""")
        self.response.out.write(sanitize(log.combined))
        self.response.out.write('<br /><br />')
        for a in log.app_logs:
          msg = a.message.decode('utf-8')
          # don't sanitize poll task URLs since they have a key= query param
          msg = util.linkify(linkify_datastore_keys(cgi.escape(
              msg if msg.startswith('Created by this poll:') else sanitize(msg))))
          self.response.out.write('%s %s %s<br />' %
              (datetime.datetime.utcfromtimestamp(a.time), LEVELS[a.level],
               msg.replace('\n', '<br />')))
        self.response.out.write('</body>\n</html>')
        return

      offset = log.offset

    self.response.out.write('No log found!')


application = webapp2.WSGIApplication([
    ('/log', LogHandler),
    ], debug=appengine_config.DEBUG)
