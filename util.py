"""Misc utility constants and classes.
"""

import urlparse

from google.appengine.api import taskqueue

import webapp2
import activitystreams.webutil.util
import activitystreams.oauth_dropins.webutil.util
import webutil.util
from webutil.util import *

EPOCH = datetime.datetime.utcfromtimestamp(0)
POLL_TASK_DATETIME_FORMAT = '%Y-%m-%d-%H-%M-%S'


def added_source_redirect(handler, source):
  """Redirects to the dashboard after adding a source.
  """
  uri = '/?added=%s#%s' % (source.key(), source.dom_id())
  uri = add_query_params(uri, [('msg', msg) for msg in handler.messages])
  handler.redirect(uri)


def add_poll_task(source, **kwargs):
  """Adds a poll task for the given source entity.
  """
  last_polled_str = source.last_polled.strftime(POLL_TASK_DATETIME_FORMAT)
  taskqueue.add(queue_name='poll',
                params={'source_key': str(source.key()),
                        'last_polled': last_polled_str},
                **kwargs)


# Wrap webutil.util.tag_uri and hard-code the year to 2013.
#
# Needed because I originally generated tag URIs with the current year, which
# resulted in different URIs for the same objects when the year changed. :/
_orig_tag_uri = webutil.util.tag_uri
webutil.util.tag_uri = lambda domain, name: _orig_tag_uri(domain, name, year=2013)
activitystreams.webutil.util.tag_uri = webutil.util.tag_uri
activitystreams.oauth_dropins.webutil.util = webutil.util.tag_uri

class Handler(webapp2.RequestHandler):
  """Includes misc request handler utilities.

  Attributes:
    messages: list of notification messages to be rendered in this page or
      wherever it redirects
  """

  def __init__(self, *args, **kwargs):
    super(Handler, self).__init__(*args, **kwargs)
    self.messages = set()

  def redirect(self, uri, **kwargs):
    """Adds self.messages to the uri as msg= query parameters.
    """
    params = urlparse.parse_qsl(urlparse.urlparse(uri).fragment)
    if self.messages and 'msg' not in params:
      uri = add_query_params(uri, [('msg', msg) for msg in self.messages])
    super(Handler, self).redirect(uri, **kwargs)
