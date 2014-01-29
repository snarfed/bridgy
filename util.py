"""Misc utility constants and classes.
"""

import urlparse

from google.appengine.api import taskqueue

import webapp2
import activitystreams.webutil.util
from activitystreams.oauth_dropins import requests
import activitystreams.oauth_dropins.webutil.util
import webutil.util
from webutil.util import *

EPOCH = datetime.datetime.utcfromtimestamp(0)
POLL_TASK_DATETIME_FORMAT = '%Y-%m-%d-%H-%M-%S'
RETRY_TASK_HTTP_STATUS = 306  # "Unused"


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


def follow_redirects(url):
  """Fetches a URL, follows redirects, and returns the final response.

  Args:
    url: string

  Returns:
    requests.Response
  """
  # can't use urllib2 since it uses GET on redirect requests, even if i specify
  # HEAD for the initial request.
  # http://stackoverflow.com/questions/9967632
  try:
    return requests.head(url, allow_redirects=True)
  except Exception, e:
    logging.warning("Couldn't resolve URL %s : %s", url, e)
    resp = requests.Response()
    resp.url = url
    resp.headers['content-type'] = 'text/html'
    return resp


# Wrap webutil.util.tag_uri and hard-code the year to 2013.
#
# Needed because I originally generated tag URIs with the current year, which
# resulted in different URIs for the same objects when the year changed. :/
_orig_tag_uri = webutil.util.tag_uri
webutil.util.tag_uri = lambda domain, name: _orig_tag_uri(domain, name, year=2013)
activitystreams.webutil.util.tag_uri = webutil.util.tag_uri
activitystreams.oauth_dropins.webutil.util = webutil.util.tag_uri


# Known domains that don't support webmentions. Mainly just the silos.
WEBMENTION_BLACKLIST = (
  'amzn.com',
  'amazon.com',
  'brid.gy',
  'brid-gy.appspot.com',
  'facebook.com',
  'm.facebook.com',
  'instagr.am',
  'instagram.com',
  'plus.google.com',
  'twitter.com',
  # these come from the text of tweets. we also pull the expanded URL
  # from the tweet entities, so ignore these instead of resolving them.
  't.co',
  'youtube.com',
  'youtu.be',
  '', None,
  )

def get_webmention_target(url):
  """Resolves a URL and decides whether we should try to send it a webmention.

  Returns: (string url, boolean) tuple. The boolean is True if we should send a
  webmention, False otherwise, e.g. if it 's a bad URL, not text/html, or in the
  blacklist.
  """
  try:
    urlparse.urlparse(url)
  except Exception, e:
    logging.warning('Dropping bad URL %s.', url)
    return (url, False)

  domain = urlparse.urlparse(url).netloc
  if domain.startswith('www.'):
    domain = domain[4:]
  if domain in WEBMENTION_BLACKLIST:
    return (url, False)

  resolved = follow_redirects(url)
  if resolved.url != url:
    logging.debug('Resolved %s to %s', url, resolved.url)
    url = resolved.url
  if not resolved.headers.get('content-type', '').startswith('text/html'):
    return (url, False)

  return (url, True)


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
