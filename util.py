# coding=utf-8
"""Misc utility constants and classes.
"""

import datetime
import urllib
import urlparse

import requests
import webapp2

from activitystreams.oauth_dropins.webutil.models import StringIdModel
from activitystreams.oauth_dropins.webutil.util import *
from activitystreams import source
from appengine_config import HTTP_TIMEOUT, DEBUG

from google.appengine.api import mail
from google.appengine.api import memcache
from google.appengine.api import taskqueue
from google.appengine.ext import ndb

EPOCH = datetime.datetime.utcfromtimestamp(0)
POLL_TASK_DATETIME_FORMAT = '%Y-%m-%d-%H-%M-%S'
FAILED_RESOLVE_URL_CACHE_TIME = 60 * 60 * 24  # a day

# Known domains that don't support webmentions. Mainly just the silos.
WEBMENTION_BLACKLIST = {
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
  }


def add_poll_task(source, **kwargs):
  """Adds a poll task for the given source entity.
  """
  last_polled_str = source.last_polled.strftime(POLL_TASK_DATETIME_FORMAT)
  taskqueue.add(queue_name='poll',
                params={'source_key': source.key.urlsafe(),
                        'last_polled': last_polled_str},
                **kwargs)


def add_propagate_task(entity, **kwargs):
  """Adds a propagate task for the given response entity.

  Tasks inserted from a backend (e.g. twitter_streaming) are sent to that
  backend by default, which doesn't work in the dev_appserver. Setting the
  target version to 'default' in queue.yaml doesn't work either, but setting it
  here does.

  Note the constant. The string 'default' works in dev_appserver, but routes to
  default.brid-gy.appspot.com in prod instead of www.brid.gy, which breaks SSL
  because appspot.com doesn't have a third-level wildcard cert.
  """
  taskqueue.add(queue_name='propagate',
                params={'response_key': entity.key.urlsafe()},
                target=taskqueue.DEFAULT_APP_VERSION)


def add_propagate_blogpost_task(entity, **kwargs):
  """Adds a propagate-blogpost task for the given response entity.
  """
  taskqueue.add(queue_name='propagate-blogpost',
                params={'key': entity.key.urlsafe()},
                target=taskqueue.DEFAULT_APP_VERSION)


def email_me(**kwargs):
  """Thin wrapper around mail.send_mail() that handles errors."""
  try:
    mail.send_mail(sender='admin@brid-gy.appspotmail.com',
                   to='webmaster@brid.gy', **kwargs)
  except BaseException:
    logging.exception('Error sending notification email')


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
    # default scheme to http
    parsed = urlparse.urlparse(url)
    if not parsed.scheme:
      url = 'http://' + url
    resolved = requests.head(url, allow_redirects=True, timeout=HTTP_TIMEOUT)
    cache_time = 0  # forever
  except AssertionError:
    raise
  except BaseException, e:
    logging.warning("Couldn't resolve URL %s : %s", url, e)
    resolved = requests.Response()
    resolved.url = url
    resolved.headers['content-type'] = 'text/html'
    resolved.status_code = 499  # not standard. i made this up.
    cache_time = FAILED_RESOLVE_URL_CACHE_TIME

  refresh = resolved.headers.get('refresh')
  if refresh:
    for part in refresh.split(';'):
      if part.strip().startswith('url='):
        return follow_redirects(part.strip()[4:])

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

  Returns: (string url, string pretty domain, boolean) tuple. The boolean is
    True if we should send a webmention, False otherwise, e.g. if it 's a bad
    URL, not text/html, in the blacklist, or can't be fetched.
  """
  try:
    domain = domain_from_link(url)
  except BaseException, e:
    logging.warning('Dropping bad URL %s.', url)
    return (url, None, False)

  if domain in WEBMENTION_BLACKLIST:
    return (url, domain, False)

  resolved = follow_redirects(url)
  if resolved.url != url:
    logging.debug('Resolved %s to %s', url, resolved.url)
    url = resolved.url
    domain = domain_from_link(url)

  is_html = resolved.headers.get('content-type', '').startswith('text/html')
  return (url, domain, is_html)


def prune_activity(activity):
  """Returns an activity dict with just id, url, content, to, and object.

  If the object field exists, it's pruned down to the same fields. Any fields
  duplicated in both the activity and the object are removed from the object.

  Note that this only prunes the to field if it says the activity is public,
  since activitystreams.Source.is_public() defaults to saying an activity is
  public if the to field is missing. If that ever changes, we'll need to
  start preserving the to field here.

  Args:
    activity: ActivityStreams activity dict

  Returns: pruned activity dict
  """
  keep = ['id', 'url', 'content']
  if not source.Source.is_public(activity):
    keep += ['to']
  pruned = {f: activity.get(f) for f in keep}

  obj = activity.get('object')
  if obj:
    obj = pruned['object'] = prune_activity(obj)
    for k, v in obj.items():
      if pruned.get(k) == v:
        del obj[k]

  return trim_nulls(pruned)


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
    """Adds self.messages to the fragment, separated by newlines.
    """
    parts = list(urlparse.urlparse(uri))
    if self.messages and not parts[5]:  # parts[5] is fragment
      parts[5] = '!' + urllib.quote('\n'.join(self.messages).encode('utf-8'))
    uri = urlparse.urlunparse(parts)
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
    if state in ('', 'listen', 'publish', 'webmention'):  # this is an add/update
      if not auth_entity:
        if not self.messages:
          self.messages.add("OK, you're not signed up. Hope you reconsider!")
        self.redirect('/')
        return

      CachedPage.invalidate('/users')
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


class CachedPage(StringIdModel):
  """Cached HTML for pages that changes rarely. Key id is path.

  Stored in the datastore since datastore entities in memcache (mostly
  Responses) are requested way more often, so it would get evicted
  out of memcache easily.

  Keys, useful for deleting from memcache:
  /: aglzfmJyaWQtZ3lyEQsSCkNhY2hlZFBhZ2UiAS8M
  /users: aglzfmJyaWQtZ3lyFgsSCkNhY2hlZFBhZ2UiBi91c2Vycww
  """
  html = ndb.TextProperty()
  expires = ndb.DateTimeProperty()

  @classmethod
  def load(cls, path):
    cached = CachedPage.get_by_id(path)
    if cached:
      if cached.expires and datetime.datetime.now() > cached.expires:
        logging.info('Deleting expired cached page for %s', path)
        cached.key.delete()
        return None
      else:
        logging.info('Found cached page for %s', path)
    return cached

  @classmethod
  def store(cls, path, html, expires=None):
    """path and html are strings, expires is a datetime.timedelta."""
    logging.info('Storing new page in cache for %s', path)
    if expires is not None:
      logging.info('  (expires in %s)', expires)
      expires = datetime.datetime.now() + expires
    CachedPage(id=path, html=html, expires=expires).put()

  @classmethod
  def invalidate(cls, path):
    logging.info('Deleting cached page for %s', path)
    CachedPage(id=path).key.delete()
