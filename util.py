"""Misc utility constants and classes.
"""

import datetime
import urllib
import urlparse

from google.appengine.api import taskqueue
from google.appengine.ext import db
import webapp2

EPOCH = datetime.datetime.utcfromtimestamp(0)
POLL_TASK_DATETIME_FORMAT = '%Y-%m-%d-%H-%M-%S'


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


def add_poll_task(source, **kwargs):
  """Adds a poll task for the given source entity.
  """
  last_polled_str = source.last_polled.strftime(POLL_TASK_DATETIME_FORMAT)
  taskqueue.add(queue_name='poll',
                params={'source_key': str(source.key()),
                        'last_polled': last_polled_str},
                **kwargs)


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


class Handler(webapp2.RequestHandler):
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
    params = (urlparse.parse_qsl(parsed[4]) +
              [('msg', msg) for msg in self.messages])
    parsed[4] = urllib.urlencode(params)
    super(Handler, self).redirect(urlparse.urlunparse(parsed), **kwargs)
