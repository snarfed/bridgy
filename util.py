"""Misc utility constants and classes.
"""

import urlparse

import requests
import webapp2

from activitystreams.oauth_dropins.webutil.util import *
from appengine_config import HTTP_TIMEOUT

from google.appengine.api import mail
from google.appengine.api import memcache
from google.appengine.api import taskqueue

EPOCH = datetime.datetime.utcfromtimestamp(0)
POLL_TASK_DATETIME_FORMAT = '%Y-%m-%d-%H-%M-%S'
FAILED_RESOLVE_URL_CACHE_TIME = 60 * 60 * 24  # a day

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
  # individual web sites that fail to fetch on app engine
  'djtymenathanscot.com',
  )


def add_poll_task(source, **kwargs):
  """Adds a poll task for the given source entity.
  """
  last_polled_str = source.last_polled.strftime(POLL_TASK_DATETIME_FORMAT)
  taskqueue.add(queue_name='poll',
                params={'source_key': source.key.urlsafe(),
                        'last_polled': last_polled_str},
                **kwargs)

def email_me(**kwargs):
  """Thin wrapper around mail.send_mail() that handles errors."""
  try:
    mail.send_mail(sender='admin@brid-gy.appspotmail.com',
                   to='webmaster@brid.gy', **kwargs)
  except BaseException, e:
    logging.exception('Error sending notification email', e)


def follow_redirects(url):
  """Fetches a URL, follows redirects, and returns the final response.

  Caches resolved URLs in memcache.

  Args:
    url: string

  Returns:
    requests.Response
  """
  cache_key = 'R ' + url
  resolved = memcache.get(cache_key)
  if resolved is not None:
    return resolved

  # can't use urllib2 since it uses GET on redirect requests, even if i specify
  # HEAD for the initial request.
  # http://stackoverflow.com/questions/9967632
  try:
    resolved = requests.head(url, allow_redirects=True, timeout=HTTP_TIMEOUT)
    cache_time = 0  # forever
  except BaseException, e:
    logging.warning("Couldn't resolve URL %s : %s", url, e)
    resolved = requests.Response()
    resolved.url = url
    resolved.headers['content-type'] = 'text/html'
    resolved.status_code = 499  # not standard. i made this up.
    cache_time = FAILED_RESOLVE_URL_CACHE_TIME

  memcache.set(cache_key, resolved, time=cache_time)
  return resolved


# Wrap webutil.util.tag_uri and hard-code the year to 2013.
#
# Needed because I originally generated tag URIs with the current year, which
# resulted in different URIs for the same objects when the year changed. :/
from activitystreams.oauth_dropins.webutil import util
_orig_tag_uri = tag_uri
util.tag_uri = lambda domain, name: _orig_tag_uri(domain, name, year=2013)


def get_webmention_target(url):
  """Resolves a URL and decides whether we should try to send it a webmention.

  Returns: (string url, boolean) tuple. The boolean is True if we should send a
  webmention, False otherwise, e.g. if it 's a bad URL, not text/html, in the
  blacklist, or can't be fetched.
  """
  try:
    urlparse.urlparse(url)
  except BaseException, e:
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
    self.messages_error = ''

  def redirect(self, uri, **kwargs):
    """Adds self.messages to the uri as msg= query parameters.
    """
    params = urlparse.parse_qsl(urlparse.urlparse(uri).fragment)
    if self.messages and 'msg' not in params:
      params += [('msg', msg) for msg in self.messages]
    if self.messages_error:
      params.append(('msg_error', self.messages_error))

    uri = add_query_params(uri, params)
    super(Handler, self).redirect(uri, **kwargs)

  def maybe_add_or_delete_source(self, source_cls, auth_entity, state):
    """Adds or deletes a source if auth_entity is not None.

    Used in each source's oauth-dropins CallbackHandler finish() and get()
    methods, respectively.

    Args:
      source_cls: source class, e.g. Instagram
      auth_entity: ouath-dropins auth entity
      state: string, OAuth callback state parameter. For adds, this is just a
        feature ('listen' or 'publish') or empty. For deletes, it's
        [FEATURE]-[SOURCE KEY].
    """
    if state is None:
      state = ''
    if state in ('', 'listen', 'publish'):  # this is an add/update
      if not auth_entity:
        self.messages.add("OK, you're not signed up. Hope you reconsider!")
        self.redirect('/')
        return

      source = source_cls.create_new(self, auth_entity=auth_entity,
                                     features=[state] if state else [])
      self.redirect(source.bridgy_url(self) if source else '/')
      return source

    else:  # this is a delete
      if auth_entity:
        self.redirect('/delete/finish?auth_entity=%s&state=%s' %
                      (auth_entity.key.urlsafe(), state))
      else:
        self.messages.add("OK, you're still signed up.")
        self.redirect(source.bridgy_url(self))

  def preprocess_source(self, source):
    """Prepares a source entity for rendering in the source.html template.

    - use id as name if name isn't provided
    - convert image URLs to https if we're serving over SSL

    Args:
      source: Source entity
    """
    if not source.name:
      source.name = source.key.string_id()
    if source.picture:
      source.picture = util.update_scheme(source.picture, self)
    return source
