# coding=utf-8
"""Misc utility constants and classes.
"""
from __future__ import unicode_literals
# use python-future's open so that it returns contents as unicode, for interop
# with webutil.util.load_file_lines().
from builtins import open

import collections
import copy
import Cookie
import contextlib
import datetime
import json
import logging
import re
import time
import urllib
import urlparse

import webapp2

from appengine_config import DEBUG
import bs4
from granary import source as gr_source
import humanize
import mf2py
from oauth_dropins.webutil import handlers as webutil_handlers
from oauth_dropins.webutil.models import StringIdModel
from oauth_dropins.webutil import util
from oauth_dropins.webutil.util import *
from webob import exc

from google.appengine.api import mail
from google.appengine.api import memcache
from google.appengine.api import taskqueue
from google.appengine.ext import ndb

# when running in dev_appserver, replace these domains in links with localhost
LOCALHOST_TEST_DOMAINS = frozenset([
  ('snarfed.org', 'localhost'),
  ('kylewm.com', 'redwind.dev'),
])

POLL_TASK_DATETIME_FORMAT = '%Y-%m-%d-%H-%M-%S'

# rate limiting errors. twitter returns 429, instagram 503, google+ 403.
# TODO: facebook. it returns 200 and reports the error in the response.
# https://developers.facebook.com/docs/reference/ads-api/api-rate-limiting/
HTTP_RATE_LIMIT_CODES = frozenset(('403', '429', '503'))

REQUEST_HEADERS = {
  'User-Agent': 'Bridgy (https://brid.gy/about)',
}
# Only send Accept header to rhiaro.co.uk right now because it needs it, but
# Known breaks on it.
# https://github.com/snarfed/bridgy/issues/713
REQUEST_HEADERS_CONNEG = copy.copy(REQUEST_HEADERS)
REQUEST_HEADERS_CONNEG['Accept'] = 'text/html, application/json; q=0.9, */*; q=0.8'
CONNEG_DOMAINS = {'rhiaro.co.uk'}
CONNEG_PATHS = {'/twitter/rhiaro'}

# alias allows unit tests to mock the function
now_fn = datetime.datetime.now

# Domains that don't support webmentions. Mainly just the silos.
# Subdomains are automatically blacklisted too.
#
# We also check this when a user sign up and we extract the web site links from
# their profile. We automatically omit links to these domains.
_dir = os.path.dirname(__file__)
with open(os.path.join(_dir, 'domain_blacklist.txt')) as f:
  BLACKLIST = util.load_file_lines(f)

# Individual URLs that we shouldn't fetch. Started because of
# https://github.com/snarfed/bridgy/issues/525 . Hopefully temporary and can be
# removed once https://github.com/idno/Known/issues/1088 is fixed!
URL_BLACKLIST = frozenset((
  'http://www.evdemon.org/2015/learning-more-about-quill',
))

# URL paths of users who opt into testing new "beta" features and changes
# before we roll them out to everyone.
with open(os.path.join(_dir, 'beta_users.txt')) as f:
  BETA_USER_PATHS = util.load_file_lines(f)

# Average HTML page size as of 2015-10-15 is 56K, so this is very generous and
# conservative.
# http://www.sitepoint.com/average-page-weight-increases-15-2014/
# http://httparchive.org/interesting.php#bytesperpage
MAX_HTTP_RESPONSE_SIZE = 500000

# Returned as the HTTP status code when an upstream API fails. Not 5xx so that
# it doesn't show up as a server error in graphs or trigger StackDriver's error
# reporting.
ERROR_HTTP_RETURN_CODE = 304  # "Not Modified"

# Returned as the HTTP status code when we refuse to make or finish a request.
HTTP_REQUEST_REFUSED_STATUS_CODE = 599

# Unpacked representation of logged in account in the logins cookie.
Login = collections.namedtuple('Login', ('site', 'name', 'path'))

canonicalize_domain = webutil_handlers.redirect(
  ('brid-gy.appspot.com', 'www.brid.gy'), 'brid.gy')

webutil_handlers.JINJA_ENV.globals.update({
  'naturaltime': humanize.naturaltime,
})


def add_poll_task(source, now=False, **kwargs):
  """Adds a poll task for the given source entity.

  Pass now=True to insert a poll-now task.
  """
  last_polled_str = source.last_polled.strftime(POLL_TASK_DATETIME_FORMAT)
  queue = 'poll-now' if now else 'poll'
  task = taskqueue.add(queue_name=queue,
                       params={'source_key': source.key.urlsafe(),
                               'last_polled': last_polled_str},
                       **kwargs)
  logging.info('Added %s task %s with args %s', queue, task.name, kwargs)


def add_propagate_task(entity, **kwargs):
  """Adds a propagate task for the given response entity."""
  task = taskqueue.add(queue_name='propagate',
                       params={'response_key': entity.key.urlsafe()},
                       **kwargs)
  logging.info('Added propagate task: %s', task.name)


def add_propagate_blogpost_task(entity, **kwargs):
  """Adds a propagate-blogpost task for the given response entity."""
  task = taskqueue.add(queue_name='propagate-blogpost',
                       params={'key': entity.key.urlsafe()},
                       **kwargs)
  logging.info('Added propagate-blogpost task: %s', task.name)

def add_discover_task(source, post_id, type=None, **kwargs):
  """Adds a propagate-blogpost task for the given source and silo post id."""
  params = {
    'source_key': source.key.urlsafe(),
    'post_id': post_id,
  }
  if type:
    params['type'] = type

  task = taskqueue.add(queue_name='discover', params=params)
  logging.info('Added discover task for post %s for %s: %s', post_id,
               source.label(), task.name)

def webmention_endpoint_cache_key(url):
  """Returns memcache key for a cached webmention endpoint for a given URL.

  Example: 'W https snarfed.org'
  """
  domain = util.domain_from_link(url)
  scheme = urlparse.urlparse(url).scheme
  return ' '.join(('W', scheme, domain))


def email_me(**kwargs):
  """Thin wrapper around :func:`mail.send_mail()` that handles errors."""
  try:
    mail.send_mail(sender='admin@brid-gy.appspotmail.com',
                   to='webmaster@brid.gy', **kwargs)
  except BaseException:
    logging.warning('Error sending notification email', exc_info=True)


def requests_get(url, **kwargs):
  """Wraps :func:`requests.get` with extra semantics and our user agent.

  If a server tells us a response will be too big (based on Content-Length), we
  hijack the response and return 599 and an error response body instead. We pass
  stream=True to :func:`requests.get` so that it doesn't fetch the response body
  until we access :attr:`requests.Response.content` (or
  :attr:`requests.Response.text`).

  http://docs.python-requests.org/en/latest/user/advanced/#body-content-workflow
  """
  if url in URL_BLACKLIST:
    resp = requests.Response()
    resp.status_code = HTTP_REQUEST_REFUSED_STATUS_CODE
    resp._text = resp._content = 'Sorry, Bridgy has blacklisted this URL.'
    return resp

  kwargs.setdefault('headers', {}).update(request_headers(url=url))
  resp = util.requests_get(url, stream=True, **kwargs)

  length = resp.headers.get('Content-Length', 0)
  if util.is_int(length) and int(length) > MAX_HTTP_RESPONSE_SIZE:
    resp.status_code = HTTP_REQUEST_REFUSED_STATUS_CODE
    resp._text = resp._content = ('Content-Length %s is larger than our limit %s.' %
                                  (length, MAX_HTTP_RESPONSE_SIZE))

  return resp


def requests_post(url, **kwargs):
  """Wraps :func:`requests.get` with our user agent."""
  kwargs.setdefault('headers', {}).update(request_headers(url=url))
  return util.requests_post(url, **kwargs)


def follow_redirects(url, cache=True):
  """Wraps :func:`oauth_dropins.webutil.util.follow_redirects` with our settings.

  ...specifically memcache and REQUEST_HEADERS.
  """
  return util.follow_redirects(url, cache=memcache if cache else None,
                               headers=request_headers(url=url))


def request_headers(url=None, source=None):
  if (url and util.domain_from_link(url) in CONNEG_DOMAINS or
      source and source.bridgy_path() in CONNEG_PATHS):
    return REQUEST_HEADERS_CONNEG

  return REQUEST_HEADERS


def get_webmention_target(url, resolve=True, replace_test_domains=True):
  """Resolves a URL and decides whether we should try to send it a webmention.

  Note that this ignores failed HTTP requests, ie the boolean in the returned
  tuple will be true! TODO: check callers and reconsider this.

  Args:
    url: string
    resolve: whether to follow redirects
    replace_test_domains: whether to replace test user domains with localhost

  Returns:
    (string url, string pretty domain, boolean) tuple. The boolean is
    True if we should send a webmention, False otherwise, e.g. if it's a bad
    URL, not text/html, or in the blacklist.
  """
  url = util.clean_url(url)
  try:
    domain = domain_from_link(url).lower()
  except BaseException:
    logging.info('Dropping bad URL %s.', url)
    return url, None, False

  send = True
  if resolve:
    # this follows *all* redirects, until the end
    resolved = follow_redirects(url, cache=memcache)
    html = resolved.headers.get('content-type', '').startswith('text/html')
    length = resolved.headers.get('Content-Length', 0)
    too_big = util.is_int(length) and int(length) > MAX_HTTP_RESPONSE_SIZE
    send = html and not too_big
    url, domain, _ = get_webmention_target(
      resolved.url, resolve=False, replace_test_domains=replace_test_domains)

  send = send and domain and not in_webmention_blacklist(domain)
  if replace_test_domains:
    url = replace_test_domains_with_localhost(url)
  return url, domain, send


def in_webmention_blacklist(domain):
  """Returns True if the domain or its root domain is in BLACKLIST."""
  return util.domain_or_parent_in(domain.lower(), BLACKLIST)


def prune_activity(activity, source):
  """Prunes an activity down to just id, url, content, to, and object, in place.

  If the object field exists, it's pruned down to the same fields. Any fields
  duplicated in both the activity and the object are removed from the object.

  Note that this only prunes the to field if it says the activity is public,
  since :meth:`granary.source.Source.is_public()` defaults to saying an activity
  is public if the to field is missing. If that ever changes, we'll need to
  start preserving the to field here.

  Args:
    activity: ActivityStreams activity dict

  Returns:
    pruned activity dict
  """
  keep = ['id', 'url', 'content', 'fb_id', 'fb_object_id', 'fb_object_type']
  if not source.is_activity_public(activity):
    keep += ['to']
  pruned = {f: activity.get(f) for f in keep}

  obj = activity.get('object')
  if obj:
    obj = pruned['object'] = prune_activity(obj, source)
    for k, v in obj.items():
      if pruned.get(k) == v:
        del obj[k]

  return trim_nulls(pruned)


def prune_response(response):
  """Returns a response object dict with a few fields removed.

  Args:
    response: ActivityStreams response object

  Returns:
    pruned response object
  """
  obj = response.get('object')
  if obj:
    response['object'] = prune_response(obj)

  drop = ['activity', 'mentions', 'originals', 'replies', 'tags']
  return trim_nulls({k: v for k, v in response.items() if k not in drop})


def replace_test_domains_with_localhost(url):
  """Replace domains in LOCALHOST_TEST_DOMAINS with localhost for local
  testing when in DEBUG mode.

  Args:
    url: a string

  Returns:
    a string with certain well-known domains replaced by localhost
  """
  if url and DEBUG:
    for test_domain, local_domain in LOCALHOST_TEST_DOMAINS:
      url = re.sub('https?://' + test_domain,
                   'http://' + local_domain, url)
  return url


class Handler(webutil_handlers.ModernHandler):
  """Includes misc request handler utilities.

  Attributes:
    messages: list of notification messages to be rendered in this page or
      wherever it redirects
  """

  def __init__(self, *args, **kwargs):
    super(Handler, self).__init__(*args, **kwargs)
    self.messages = set()
    self.response.headers['Content-Security-Policy'] = \
      self.response.headers['Content-Security-Policy'].replace(
        "frame-ancestors 'self';",
        "frame-ancestors 'self' https://www.facebook.com/ https://apps.facebook.com/;")
    # X-Frame-Options ALLOW-FROM doesn't allow multiple domains, so just drop it.
    # http://stackoverflow.com/questions/10205192
    del self.response.headers['X-Frame-Options']

  def redirect(self, uri, **kwargs):
    """Adds self.messages to the fragment, separated by newlines."""
    parts = list(urlparse.urlparse(uri))
    if self.messages and not parts[5]:  # parts[5] is fragment
      parts[5] = '!' + urllib.quote('\n'.join(self.messages).encode('utf-8'))
    uri = urlparse.urlunparse(parts)
    super(Handler, self).redirect(uri, **kwargs)

  def maybe_add_or_delete_source(self, source_cls, auth_entity, state, **kwargs):
    """Adds or deletes a source if auth_entity is not None.

    Used in each source's oauth-dropins :meth:`CallbackHandler.finish()` and
    :meth:`CallbackHandler.get()` methods, respectively.

    Args:
      source_cls: source class, e.g. :class:`instagram.Instagram`
      auth_entity: ouath-dropins auth entity
      state: string, OAuth callback state parameter. a JSON serialized dict
        with operation, feature, and an optional callback URL. For deletes,
        it will also include the source key
      kwargs: passed through to the source_cls constructor

    Returns:
      source entity if it was created or updated, otherwise None
    """
    state_obj = util.decode_oauth_state(state)
    operation = state_obj.get('operation', 'add')
    feature = state_obj.get('feature')
    callback = state_obj.get('callback')
    user_url = state_obj.get('user_url')

    logging.debug(
      'maybe_add_or_delete_source with operation=%s, feature=%s, callback=%s',
      operation, feature, callback)

    if operation == 'add':  # this is an add/update
      if not auth_entity:
        if not self.messages:
          self.messages.add("OK, you're not signed up. Hope you reconsider!")
        if callback:
          callback = util.add_query_params(callback, {'result': 'declined'})
          logging.debug(
            'user declined adding source, redirect to external callback %s',
            callback)
          # call super.redirect so the callback url is unmodified
          super(Handler, self).redirect(callback.encode('utf-8'))
        else:
          self.redirect('/')
        return

      CachedPage.invalidate('/users')
      logging.info('%s.create_new with %s', source_cls.__class__.__name__,
                   (auth_entity.key, state, kwargs))
      source = source_cls.create_new(self, auth_entity=auth_entity,
                                     features=feature.split(',') if feature else [],
                                     user_url=user_url, **kwargs)

      if source:
        # add to login cookie
        logins = self.get_logins()
        logins.append(Login(path=source.bridgy_path(), site=source.SHORT_NAME,
                            name=source.label_name()))
        self.set_logins(logins)

      if callback:
        callback = util.add_query_params(callback, {
          'result': 'success',
          'user': source.bridgy_url(self),
          'key': source.key.urlsafe(),
        } if source else {'result': 'failure'})
        logging.debug(
          'finished adding source, redirect to external callback %s', callback)
        # call super.redirect so the callback url is unmodified
        super(Handler, self).redirect(callback.encode('utf-8'))
      else:
        self.redirect(source.bridgy_url(self) if source else '/')
      return source

    else:  # this is a delete
      if auth_entity:
        self.redirect('/delete/finish?auth_entity=%s&state=%s' %
                      (auth_entity.key.urlsafe(), state))
      else:
        self.messages.add('If you want to disable, please approve the %s prompt.' %
                          source_cls.GR_CLASS.NAME)
        source_key = state.get('source')
        if source_key:
          source = ndb.Key(urlsafe=source_key).get()
          if source:
            return self.redirect(source.bridgy_url(self))

        self.redirect('/')

  def construct_state_param_for_add(self, state=None, **kwargs):
    """Construct the state parameter if one isn't explicitly passed in.

    The following keys are common:
    - operation: 'add' or 'delete'
    - feature: 'listen', 'publish', or 'webmention'
    - callback: an optional external callback, that we will redirect to at
                the end of the authorization handshake
    - source: the source key, only applicable to deletes
    """
    state_obj = util.decode_oauth_state(state)
    if not state_obj:
      state_obj = {field: self.request.get(field) for field in
                   ('callback', 'feature', 'id', 'user_url')}
      state_obj['operation'] = 'add'

    if kwargs:
      state_obj.update(kwargs)

    return util.encode_oauth_state(state_obj)

  def get_logins(self):
    """Extracts the current user page paths from the logins cookie.

    Returns:
      list of :class:`Login` objects
    """
    cookie = self.request.headers.get('Cookie', '')
    if cookie:
      logging.info('Cookie: %s', cookie)

    try:
      logins_str = Cookie.SimpleCookie(cookie).get('logins')
    except Cookie.CookieError, e:
      logging.warning("Bad cookie: %s", e)
      return []

    if not logins_str or not logins_str.value:
      return []

    logins = []
    for val in set(urllib.unquote_plus(logins_str.value).decode('utf-8').split('|')):
      parts = val.split('?', 1)
      path = parts[0]
      if not path:
        continue
      name = parts[1] if len(parts) > 1 else ''
      site, _ = path.strip('/').split('/')
      logins.append(Login(path=path, site=site, name=name))

    return logins

  def set_logins(self, logins):
    """Sets a logins cookie.

    Args:
      logins: sequence of :class:`Login` objects
    """
    # cookie docs: http://curl.haxx.se/rfc/cookie_spec.html
    cookie = Cookie.SimpleCookie()
    cookie['logins'] = '|'.join(sorted(set(
      '%s?%s' % (login.path, urllib.quote_plus(login.name.encode('utf-8')))
      for login in logins)))
    cookie['logins']['path'] = '/'
    cookie['logins']['expires'] = now_fn() + datetime.timedelta(days=365 * 2)

    header = cookie['logins'].OutputString()
    logging.info('Set-Cookie: %s', header)
    self.response.headers['Set-Cookie'] = header

  def preprocess_source(self, source):
    """Prepares a source entity for rendering in the source.html template.

    - use id as name if name isn't provided
    - convert image URLs to https if we're serving over SSL
    - set 'website_links' attr to list of pretty HTML links to domain_urls

    Args:
      source: :class:`models.Source` entity
    """
    if not source.name:
      source.name = source.key.string_id()
    if source.picture:
      source.picture = util.update_scheme(source.picture, self)
    source.website_links = [
      util.pretty_link(url, attrs={'rel': 'me', 'class': 'u-url'})
      for url in source.domain_urls]
    return source


def oauth_starter(oauth_start_handler, **kwargs):
  """Returns an oauth-dropins start handler that injects the state param.

  Args:
    oauth_start_handler: oauth-dropins :class:`StartHandler` to use,
      e.g. :class:`oauth_dropins.twitter.StartHandler`.
    kwargs: passed to :meth:`construct_state_param_for_add()`
  """
  class StartHandler(oauth_start_handler, Handler):
    def redirect_url(self, state=None, **ru_kwargs):
      return super(StartHandler, self).redirect_url(
        self.construct_state_param_for_add(state, **kwargs), **ru_kwargs)

  return StartHandler


class CachedPage(StringIdModel):
  """Cached HTML for pages that changes rarely. Key id is path.

  Stored in the datastore since datastore entities in memcache (mostly
  :class:`models.Response`) are requested way more often, so it would get
  evicted out of memcache easily.

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
      if cached.expires and now_fn() > cached.expires:
        logging.info('Deleting expired cached page for %s', path)
        cached.key.delete()
        return None
      else:
        logging.info('Found cached page for %s', path)
    return cached

  @classmethod
  def store(cls, path, html, expires=None):
    """Stores new page contents.

    Args:
      path: string
      html: string
      expires: :class:`datetime.timedelta`
    """
    logging.info('Storing new page in cache for %s', path)
    if expires is not None:
      logging.info('  (expires in %s)', expires)
      expires = now_fn() + expires
    CachedPage(id=path, html=html, expires=expires).put()

  @classmethod
  def invalidate(cls, path):
    logging.info('Deleting cached page for %s', path)
    CachedPage(id=path).key.delete()


def unwrap_t_umblr_com(url):
  """If url is a t.umblr.com short link, extract its destination URL.

  Otherwise, return url unchanged.

  Not in tumblr.py since models imports superfeedr, so it would be a circular
  import.

  Background: https://github.com/snarfed/bridgy/issues/609
  """
  parsed = urlparse.urlparse(url)
  return (urlparse.parse_qs(parsed.query).get('z', [''])[0]
          if parsed.netloc == 't.umblr.com'
          else url)


@contextlib.contextmanager
def cache_time(label, size=None):
  """Times a block of code, logs the time, and aggregates it in memcache."""
  start = int(time.clock() * 1000)
  yield
  elapsed = int(time.clock() * 1000) - start

  logging.info('Parse time for %s: %dms', label, elapsed)
  memcache.incr('timed %s' % label, elapsed, initial_value=0)
  if size:
    memcache.incr('timed %s size' % label, size, initial_value=0)


def beautifulsoup_parse(html):
  """Parses an HTML string with BeautifulSoup. Centralizes our parsing config.

  We currently use lxml, which BeautifulSoup claims is the fastest and best:
  http://www.crummy.com/software/BeautifulSoup/bs4/doc/#specifying-the-parser-to-use

  lxml is a native module, so we don't bundle and deploy it to App Engine.
  Instead, we use App Engine's version by declaring it in app.yaml.
  https://cloud.google.com/appengine/docs/standard/python/tools/built-in-libraries-27

  We pin App Engine's version, 3.7.3, in requirements.freeze.txt, and tell
  BeautifulSoup to use lxml explicitly, to ensure we use the same parser and
  version in prod and locally, since we've been bit by at least one meaningful
  difference between lxml and e.g. html5lib: lxml includes the contents of
  <noscript> tags, html5lib omits them. :(
  https://github.com/snarfed/bridgy/issues/798#issuecomment-370508015
  """
  # instrumenting, disabled for now:
  # with cache_time('beautifulsoup', len(html)):
  return bs4.BeautifulSoup(html, 'lxml')


def mf2py_parse(input, url):
  """Uses mf2py to parse an input HTML string or BeautifulSoup input."""
  if isinstance(input, basestring):
    input = beautifulsoup_parse(input)

  # instrumenting, disabled for now:
  # with cache_time('mf2py', 1):
  return mf2py.parse(url=url, doc=input)
