"""Misc utility constants and classes.
"""

import cgi
import datetime
import urllib
import urlparse

from google.appengine.ext import db
from google.appengine.ext import webapp

EPOCH = datetime.datetime.utcfromtimestamp(0)


def reduce_url(url):
  """Removes a URL's leading scheme (e.g. http://) and trailing slash.
  """
  parsed = urlparse.urlparse(url)
  reduced = parsed.netloc
  if parsed.path:
    reduced += parsed.path
  if reduced.endswith('/'):
    reduced = reduced[:-1]
  return reduced


def favicon_for_url(url):
  return 'http://%s/favicon.ico' % urlparse.urlparse(url).netloc


class KeyNameModel(db.Model):
  """A model class that requires a key name.
  """

  def __init__(self, *args, **kwargs):
    """Raises AssertionError if key name is not provided."""
    super(KeyNameModel, self).__init__(*args, **kwargs)
    try:
      assert self.key().name()
    except db.NotSavedError:
      assert False, 'key name required but not provided'


class Handler(webapp.RequestHandler):
  """Includes misc request handler utilities.

  Attributes:
    messages: list of notification messages to be rendered in this page or
      wherever it redirects
  """

  def __init__(self, *args, **kwargs):
    super(Handler, self).__init__(*args, **kwargs)
    self.messages = []

  def redirect(self, uri, **kwargs):
    """Adds self.messages to the uri as msg= query parameters.
    """
    parsed = list(urlparse.urlparse(uri))
    # query params are in index 4
    # TODO: when this is on python 2.7, switch to urlparse.parse_qsl
    params = (cgi.parse_qsl(parsed[4]) +
              [('msg', msg) for msg in self.messages])
    parsed[4] = urllib.urlencode(params)
    super(Handler, self).redirect(urlparse.urlunparse(parsed), **kwargs)
