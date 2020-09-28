# coding=utf-8
"""Misc utility constants and classes.
"""
import binascii
import collections
import copy
from http.cookies import CookieError, SimpleCookie
import contextlib
import datetime
import logging
import random
import re
import threading
import time
import urllib.request, urllib.parse, urllib.error
import zlib

from cachetools import TTLCache
from google.cloud import ndb
from google.cloud.tasks_v2 import CreateTaskRequest
from google.protobuf.timestamp_pb2 import Timestamp
import google.protobuf.message
import humanize
from oauth_dropins.webutil.appengine_config import error_reporting_client, tasks_client
from oauth_dropins.webutil.appengine_info import APP_ID, LOCAL
from oauth_dropins.webutil import handlers as webutil_handlers
from oauth_dropins.webutil.models import StringIdModel
from oauth_dropins.webutil import util
from oauth_dropins.webutil.util import *

# when running in dev_appserver, replace these domains in links with localhost
LOCALHOST_TEST_DOMAINS = frozenset([
  ('snarfed.org', 'localhost'),
  ('kylewm.com', 'redwind.dev'),
])

LOCAL_HOSTS = {'localhost', '127.0.0.1'}

POLL_TASK_DATETIME_FORMAT = '%Y-%m-%d-%H-%M-%S'

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
# Subdomains are automatically blocklisted too.
#
# We also check this when a user sign up and we extract the web site links from
# their profile. We automatically omit links to these domains.
_dir = os.path.dirname(__file__)
with open(os.path.join(_dir, 'domain_blocklist.txt'), 'rt', encoding='utf-8') as f:
  BLOCKLIST = util.load_file_lines(f)

# Individual URLs that we shouldn't fetch. Started because of
# https://github.com/snarfed/bridgy/issues/525 . Hopefully temporary and can be
# removed once https://github.com/idno/Known/issues/1088 is fixed!
URL_BLOCKLIST = frozenset((
  'http://www.evdemon.org/2015/learning-more-about-quill',
))

# URL paths of users who opt into testing new "beta" features and changes
# before we roll them out to everyone.
with open(os.path.join(_dir, 'beta_users.txt'), 'rt', encoding='utf-8') as f:
  BETA_USER_PATHS = util.load_file_lines(f)

# Returned as the HTTP status code when an upstream API fails. Not 5xx so that
# it doesn't show up as a server error in graphs or trigger StackDriver's error
# reporting.
ERROR_HTTP_RETURN_CODE = 304  # "Not Modified"

# Returned as the HTTP status code when we refuse to make or finish a request.
HTTP_REQUEST_REFUSED_STATUS_CODE = 599

# Unpacked representation of logged in account in the logins cookie.
Login = collections.namedtuple('Login', ('site', 'name', 'path'))

HOST_URL = 'https://brid.gy'
PRIMARY_DOMAIN = 'brid.gy'
OTHER_DOMAINS = (
  'background.brid-gy.appspot.com',
  'default.brid-gy.appspot.com',
  'brid-gy.appspot.com',
  'www.brid.gy',
  'bridgy.org',
  'www.bridgy.org',
)
LOCAL_DOMAINS = (
  'localhost:8080',
  'my.dev.com:8080',
)
DOMAINS = (PRIMARY_DOMAIN,) + OTHER_DOMAINS + LOCAL_DOMAINS
canonicalize_domain = webutil_handlers.redirect(OTHER_DOMAINS, PRIMARY_DOMAIN)

webutil_handlers.JINJA_ENV.globals.update({
  'naturaltime': humanize.naturaltime,
})

# https://cloud.google.com/appengine/docs/locations
TASKS_LOCATION = 'us-central1'

webmention_endpoint_cache_lock = threading.RLock()
webmention_endpoint_cache = TTLCache(500, 60 * 60 * 2)  # 2h expiration


def add_poll_task(source, now=False):
  """Adds a poll task for the given source entity.

  Pass now=True to insert a poll-now task.
  """
  if now:
    queue = 'poll-now'
    eta_seconds = None
  else:
    queue = 'poll'
    # randomize task ETA to within +/- 20% to try to spread out tasks and
    # prevent thundering herds.
    eta_seconds = int(util.to_utc_timestamp(now_fn()) +
                      source.poll_period().total_seconds() * random.uniform(.8, 1.2))

  add_task(queue, eta_seconds=eta_seconds, source_key=source.key.urlsafe().decode(),
           last_polled=source.last_polled.strftime(POLL_TASK_DATETIME_FORMAT))


def add_propagate_task(entity):
  """Adds a propagate task for the given response entity."""
  add_task('propagate', response_key=entity.key.urlsafe().decode())


def add_propagate_blogpost_task(entity):
  """Adds a propagate-blogpost task for the given response entity."""
  add_task('propagate-blogpost', key=entity.key.urlsafe().decode())


def add_discover_task(source, post_id, type=None):
  """Adds a discover task for the given source and silo post id."""
  add_task('discover', source_key=source.key.urlsafe().decode(),
           post_id=post_id, type=type)


def add_task(queue, eta_seconds=None, **kwargs):
  """Adds a Cloud Tasks task for the given entity.

  Args:
    queue: string, queue name
    entity: Source or Webmentions instance
    eta_seconds: integer, optional
    kwargs: added to task's POST body (form-encoded)
  """
  params = {
    'app_engine_http_request': {
      'http_method': 'POST',
      'relative_uri': '/_ah/queue/%s' % queue,
      'app_engine_routing': {'service': 'background'},
      'body': urllib.parse.urlencode(util.trim_nulls(kwargs)).encode(),
      # https://googleapis.dev/python/cloudtasks/latest/gapic/v2/types.html#google.cloud.tasks_v2.types.AppEngineHttpRequest.headers
      'headers': {'Content-Type': 'application/x-www-form-urlencoded'},
    }
  }
  if eta_seconds:
    params['schedule_time'] = Timestamp(seconds=eta_seconds)

  queue_path = tasks_client.queue_path(APP_ID, TASKS_LOCATION, queue)
  if LOCAL:
    logging.info('Would add task: %s %s', queue_path, params)
  else:
    task = tasks_client.create_task(CreateTaskRequest(parent=queue_path, task=params))
    logging.info('Added %s task %s with ETA %s', queue, task.name, eta_seconds)


def webmention_endpoint_cache_key(url):
  """Returns cache key for a cached webmention endpoint for a given URL.

  Example: 'W https snarfed.org /'

  If the URL is the home page, ie path is / , the key includes a / at the end,
  so that we cache webmention endpoints for home pages separate from other pages.
  https://github.com/snarfed/bridgy/issues/701
  """
  domain = util.domain_from_link(url)
  scheme = urllib.parse.urlparse(url).scheme

  parts = ['W', scheme, domain]
  if urllib.parse.urlparse(url).path in ('', '/'):
    parts.append('/')

  return ' '.join(parts)


def report_error(msg, **kwargs):
  """Reports an error to StackDriver Error Reporting.

  https://cloud.google.com/error-reporting/docs/reference/libraries#client-libraries-install-python

  Args:
    msg: string
  """
  try:
    error_reporting_client.report(msg, **kwargs)
  except BaseException:
    logging.warning('Failed to report error to StackDriver! %s %s', msg, kwargs,
                    stack_info=True)


def requests_get(url, **kwargs):
  """Wraps :func:`requests.get` with extra semantics and our user agent.

  If a server tells us a response will be too big (based on Content-Length), we
  hijack the response and return 599 and an error response body instead. We pass
  stream=True to :func:`requests.get` so that it doesn't fetch the response body
  until we access :attr:`requests.Response.content` (or
  :attr:`requests.Response.text`).

  http://docs.python-requests.org/en/latest/user/advanced/#body-content-workflow
  """
  host = urllib.parse.urlparse(url).netloc.split(':')[0]
  if url in URL_BLOCKLIST or (not LOCAL and host in LOCAL_HOSTS):
    resp = requests.Response()
    resp.status_code = HTTP_REQUEST_REFUSED_STATUS_CODE
    resp._text = 'Sorry, Bridgy has blocklisted this URL.'
    resp._content = resp._text.encode()
    return resp

  kwargs.setdefault('headers', {}).update(request_headers(url=url))
  return util.requests_get(url, **kwargs)


def fetch_mf2(url, **kwargs):
  """Injects :func:`requests_get` into :func:`oauth_dropins.webutil.util.fetch_mf2`."""
  return util.fetch_mf2(url, get_fn=requests_get, **kwargs)


def requests_post(url, **kwargs):
  """Wraps :func:`requests.get` with our user agent."""
  kwargs.setdefault('headers', {}).update(request_headers(url=url))
  return util.requests_post(url, **kwargs)


def follow_redirects(url):
  """Wraps :func:`oauth_dropins.webutil.util.follow_redirects` with our headers."""
  return util.follow_redirects(url, headers=request_headers(url=url))


def request_headers(url=None, source=None):
  if (url and util.domain_from_link(url) in CONNEG_DOMAINS or
      source and source.bridgy_path() in CONNEG_PATHS):
    return REQUEST_HEADERS_CONNEG

  return REQUEST_HEADERS


def get_webmention_target(url, resolve=True, replace_test_domains=True):
  """Resolves a URL and decides whether we should try to send it a webmention.

  Note that this ignores failed HTTP requests, ie the boolean in the returned
  tuple will be True! TODO: check callers and reconsider this.

  Args:
    url: string
    resolve: whether to follow redirects
    replace_test_domains: whether to replace test user domains with localhost

  Returns:
    (string url, string pretty domain, boolean) tuple. The boolean is
    True if we should send a webmention, False otherwise, e.g. if it's a bad
    URL, not text/html, or in the blocklist.
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
    resolved = follow_redirects(url)
    html = resolved.headers.get('content-type', '').startswith('text/html')
    send = html and resolved.status_code != util.HTTP_RESPONSE_TOO_BIG_STATUS_CODE
    url, domain, _ = get_webmention_target(
      resolved.url, resolve=False, replace_test_domains=replace_test_domains)

  scheme = urllib.parse.urlparse(url).scheme  # require http or https
  send = (send and domain and scheme in ('http', 'https') and
          not in_webmention_blocklist(domain))

  if replace_test_domains:
    url = replace_test_domains_with_localhost(url)

  return url, domain, send


def in_webmention_blocklist(domain):
  """Returns True if the domain or its root domain is in BLOCKLIST."""
  domain = domain.lower()
  return (util.domain_or_parent_in(domain, BLOCKLIST) or
          (not LOCAL and domain in LOCAL_HOSTS))


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
    for k, v in list(obj.items()):
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
  """Replace domains in LOCALHOST_TEST_DOMAINS with localhost for local testing.

  Args:
    url: a string

  Returns:
    a string with certain well-known domains replaced by localhost
  """
  if url and LOCAL:
    for test_domain, local_domain in LOCALHOST_TEST_DOMAINS:
      url = re.sub('https?://' + test_domain,
                   'http://' + local_domain, url)
  return url


def host_url(handler):
  domain = util.domain_from_link(handler.request.host_url)
  return HOST_URL if domain in OTHER_DOMAINS else handler.request.host_url


def load_source(handler, param='source_key'):
  """Extracts a URL-safe key from a query parameter and loads a source object.

  Returns HTTP 400 if the parameter is not provided or the source doesn't exist.

  Args:
    handler: RequestHandler
    param: string

  Returns: Source object
  """
  try:
    source = ndb.Key(urlsafe=util.get_required_param(handler, param)).get()
  except (binascii.Error, google.protobuf.message.DecodeError):
    msg = 'Bad value for %s' % param
    logging.warning(msg, stack_info=True)
    handler.abort(400, msg)

  if not source:
    handler.abort(400, 'Source key not found')

  return source


class Handler(webutil_handlers.ModernHandler):
  """Includes misc request handler utilities.

  Attributes:
    messages: list of notification messages to be rendered in this page or
      wherever it redirects
  """

  def __init__(self, *args, **kwargs):
    super(Handler, self).__init__(*args, **kwargs)
    self.messages = set()

  def redirect(self, uri, **kwargs):
    """Adds self.messages to the fragment, separated by newlines."""
    parts = list(urllib.parse.urlparse(uri))
    if self.messages and not parts[5]:  # parts[5] is fragment
      parts[5] = '!' + urllib.parse.quote('\n'.join(self.messages).encode())
    uri = urllib.parse.urlunparse(parts)
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
          super(Handler, self).redirect(callback)
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
          'key': source.key.urlsafe().decode(),
        } if source else {'result': 'failure'})
        logging.debug(
          'finished adding source, redirect to external callback %s', callback)
        # call super.redirect so the callback url is unmodified
        super(Handler, self).redirect(callback)

      elif source and not source.domains:
        self.redirect('/edit-websites?' + urllib.parse.urlencode({
          'source_key': source.key.urlsafe().decode(),
        }))

      else:
        self.redirect(source.bridgy_url(self) if source else '/')

      return source

    else:  # this is a delete
      if auth_entity:
        self.redirect('/delete/finish?auth_entity=%s&state=%s' %
                      (auth_entity.key.urlsafe().decode(), state))
      else:
        self.messages.add('If you want to disable, please approve the %s prompt.' %
                          source_cls.GR_CLASS.NAME)
        source_key = state_obj.get('source')
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
      logins_str = SimpleCookie(cookie).get('logins')
    except CookieError as e:
      logging.warning("Bad cookie: %s", e)
      return []

    if not logins_str or not logins_str.value:
      return []

    logins = []
    for val in set(urllib.parse.unquote_plus(logins_str.value).split('|')):
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
    cookie = SimpleCookie()
    cookie['logins'] = '|'.join(sorted(set(
      '%s?%s' % (login.path, urllib.parse.quote_plus(login.name))
      for login in logins)))
    cookie['logins']['path'] = '/'

    expires = (now_fn() + datetime.timedelta(days=365 * 2)).replace(microsecond=0)
    # this will have a space in it, eg '2021-12-08 15:48:34', so quote it
    cookie['logins']['expires'] = '"%s"' % expires

    header = cookie['logins'].OutputString()
    logging.info('Set-Cookie: %s', header)
    self.response.headers['Set-Cookie'] = header

  def preprocess_source(self, source):
    """Prepares a source entity for rendering in the source.html template.

    - convert image URLs to https if we're serving over SSL
    - set 'website_links' attr to list of pretty HTML links to domain_urls

    Args:
      source: :class:`models.Source` entity
    """
    if source.picture:
      source.picture = util.update_scheme(source.picture, self)
    source.website_links = [
      util.pretty_link(url, attrs={'rel': 'me', 'class': 'u-url'})
      for url in source.domain_urls]
    return source

  def load_source(self, **kwargs):
    return load_source(self, **kwargs)


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
  parsed = urllib.parse.urlparse(url)
  return (urllib.parse.parse_qs(parsed.query).get('z', [''])[0]
          if parsed.netloc == 't.umblr.com'
          else url)


def background_handle_exception(handler, e, debug):
  """Common exception handler for background tasks.

  Catches failed outbound HTTP requests and returns HTTP 304.

  Install with eg:

  class MyHandler(webapp2.RequestHandler):
    handle_exception = util.background_handle_exception
    ...
  """
  transients = getattr(handler, 'TRANSIENT_ERROR_HTTP_CODES', ())
  source = getattr(handler, 'source', None)
  if source:
    transients += source.RATE_LIMIT_HTTP_CODES + source.TRANSIENT_ERROR_HTTP_CODES

  code, body = util.interpret_http_exception(e)
  if ((code and int(code) // 100 == 5) or code in transients or
      util.is_connection_failure(e)):
    logging.error('Marking as error and finishing. %s: %s\n%s', code, body, e)
    handler.abort(ERROR_HTTP_RETURN_CODE)
  else:
    raise


class NoopHandler(Handler):
  """Returns 200 and does nothing. Useful for /_ah/start, /_ah/stop, etc.

  https://cloud.google.com/appengine/docs/standard/python3/how-instances-are-managed#startup
  """

  def get(self, *args):
    pass

  def post(self, *args):
    pass
