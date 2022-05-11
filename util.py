# coding=utf-8
"""Misc utility constants and classes.
"""
import binascii
import collections
import copy
from datetime import datetime, timedelta, timezone
import logging
import os
import random
import re
import threading
import urllib.request, urllib.parse, urllib.error

from cachetools import TTLCache
import flask
from flask import request
from google.cloud import ndb
from google.cloud.tasks_v2 import CreateTaskRequest
from google.protobuf.timestamp_pb2 import Timestamp
import google.protobuf.message
from oauth_dropins.webutil.appengine_config import error_reporting_client, tasks_client
from oauth_dropins.webutil.appengine_info import APP_ID, DEBUG, LOCAL
from oauth_dropins.webutil.flask_util import error, flash
from oauth_dropins.webutil import util
from oauth_dropins.webutil.util import *
import requests
from werkzeug.routing import RequestRedirect

logger = logging.getLogger(__name__)

# when running locally, replace these domains in links with localhost
LOCALHOST_TEST_DOMAINS = frozenset([
  ('snarfed.org', 'localhost'),
  ('kylewm.com', 'redwind.dev'),
])

LOCAL_HOSTS = {'localhost', '127.0.0.1'}

POLL_TASK_DATETIME_FORMAT = '%Y-%m-%d-%H-%M-%S'

# Only send Accept header to rhiaro.co.uk right now because it needs it, but
# Known breaks on it.
# https://github.com/snarfed/bridgy/issues/713
REQUEST_HEADERS_CONNEG = {'Accept': 'text/html, application/json; q=0.9, */*; q=0.8'}
CONNEG_DOMAINS = {'rhiaro.co.uk'}
CONNEG_PATHS = {'/twitter/rhiaro'}

# alias allows unit tests to mock the function
now_fn = lambda: datetime.now(tz=timezone.utc)

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

# https://cloud.google.com/appengine/docs/locations
TASKS_LOCATION = 'us-central1'

webmention_endpoint_cache_lock = threading.RLock()
webmention_endpoint_cache = TTLCache(5000, 60 * 60 * 2)  # 2h expiration


def add_poll_task(source, now=False):
  """Adds a poll task for the given source entity.

  Pass now=True to insert a poll-now task.
  """
  if now:
    queue = 'poll-now'
    eta_seconds = None
  else:
    queue = 'poll'
    eta_seconds = int(util.to_utc_timestamp(now_fn()))
    if source.AUTO_POLL:
      # add poll period. randomize task ETA to within +/- 20% to try to spread
      # out tasks and prevent thundering herds.
      eta_seconds += int(source.poll_period().total_seconds() * random.uniform(.8, 1.2))

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
      'relative_uri': f'/_ah/queue/{queue}',
      'body': urllib.parse.urlencode(util.trim_nulls(kwargs)).encode(),
      # https://googleapis.dev/python/cloudtasks/latest/gapic/v2/types.html#google.cloud.tasks_v2.types.AppEngineHttpRequest.headers
      'headers': {'Content-Type': 'application/x-www-form-urlencoded'},
    }
  }
  if eta_seconds:
    params['schedule_time'] = Timestamp(seconds=eta_seconds)

  queue_path = tasks_client.queue_path(APP_ID, TASKS_LOCATION, queue)
  if LOCAL:
    logger.info(f'Would add task: {queue_path} {params}')
  else:
    task = tasks_client.create_task(CreateTaskRequest(parent=queue_path, task=params))
    logger.info(f'Added {queue} task {task.name} with ETA {eta_seconds}')


class Redirect(RequestRedirect):
  """Adds login cookie support to :class:`werkzeug.exceptions.RequestRedirect`."""
  logins = None

  def get_response(self, *args, **kwargs):
    resp = super().get_response()

    if self.logins is not None:
      # cookie docs: http://curl.haxx.se/rfc/cookie_spec.html
      cookie = '|'.join(sorted(
        {f'{login.path}?{urllib.parse.quote_plus(login.name)}'
         for login in self.logins}))

      logger.info(f'setting logins cookie: {cookie}')
      age = timedelta(days=365 * 2)
      expires = (now_fn() + age).replace(microsecond=0)
      resp.set_cookie('logins', cookie, max_age=age, expires=expires)

    return resp


def redirect(path, code=302, logins=None):
  """Stops execution and redirects to the absolute URL for a given path.

  Specifically, raises :class:`werkzeug.routing.RequestRedirect`.

  Args:
    url: str
    code: int, HTTP status code
    logins: optional, list of :class:`util.Login` to be set in a Set-Cookie HTTP
      header
  """
  logger.info(f'Redirecting to {path}')
  rr = Redirect(host_url(path))
  rr.code = code
  rr.logins = logins
  raise rr


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
    if not DEBUG:
      logger.warning(f'Failed to report error to StackDriver! {msg} {kwargs}', exc_info=True)


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

  return {}


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
    logger.info(f'Dropping bad URL: {url!r}.')
    return url, None, False

  send = True
  if resolve:
    # this follows *all* redirects, until the end
    resolved = follow_redirects(url)
    html = (resolved.headers.get('content-type', '').split(';')[0]
            in ('text/html', 'text/mf2+html'))
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


def host_url(path_query=None):
  domain = util.domain_from_link(request.host_url)
  base = (HOST_URL if util.domain_or_parent_in(domain, OTHER_DOMAINS)
          else request.host_url)
  return urllib.parse.urljoin(base, path_query)


def load_source(error_fn=None):
  """Loads a source from the `source_key` or `key` query parameter.

  Expects the query parameter value to be a URL-safe key. Returns HTTP 400 if
  neither parameter is provided or the source doesn't exist.

  Args:
    error_fn: callable to be called with errors. Takes one parameter, the string
      error message.

  Returns: :class:`models.Source`
  """
  logger.debug(f'Params: {list(request.values.items())}')
  if error_fn is None:
    error_fn = error

  for param in 'source_key', 'key':
    try:
      val = request.values.get(param)
      if val:
        source = ndb.Key(urlsafe=val).get()
        if source:
          return source
    except (binascii.Error, google.protobuf.message.DecodeError):
      error_fn(f'Bad value for {param}')

  error_fn('Source key not found')


def maybe_add_or_delete_source(source_cls, auth_entity, state, **kwargs):
  """Adds or deletes a source if auth_entity is not None.

  Used in each source's oauth-dropins :meth:`Callback.finish()` and
  :meth:`Callback.get()` methods, respectively.

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

  logger.debug(
    'maybe_add_or_delete_source with operation=%s, feature=%s, callback=%s',
    operation, feature, callback)
  logins = None

  if operation == 'add':  # this is an add/update
    if not auth_entity:
      # TODO: only show if we haven't already flashed another message?
      # get_flashed_messages() caches so it's dangerous to call to check;
      # use eg session.get('_flashes', []) instead.
      # https://stackoverflow.com/a/17243946/186123
      flash("OK, you're not signed up. Hope you reconsider!")
      if callback:
        callback = util.add_query_params(callback, {'result': 'declined'})
        logger.debug(
          f'user declined adding source, redirect to external callback {callback}')
        redirect(callback)
      else:
        redirect('/')

    logger.info(f'{source_cls.__class__.__name__}.create_new with {auth_entity.key}, {state}, {kwargs}')
    source = source_cls.create_new(auth_entity=auth_entity,
                                   features=feature.split(',') if feature else [],
                                   user_url=user_url, **kwargs)

    if source:
      # add to login cookie
      logins = get_logins()
      logins.append(Login(path=source.bridgy_path(), site=source.SHORT_NAME,
                          name=source.label_name()))

    if callback:
      callback = util.add_query_params(callback, {
        'result': 'success',
        'user': source.bridgy_url(),
        'key': source.key.urlsafe().decode(),
      } if source else {'result': 'failure'})
      logger.debug(
        'finished adding source, redirect to external callback %s', callback)
      redirect(callback, logins=logins)

    elif source and not source.domains:
      redirect('/edit-websites?' + urllib.parse.urlencode({
        'source_key': source.key.urlsafe().decode(),
      }), logins=logins)

    else:
      redirect(source.bridgy_url() if source else '/', logins=logins)

  else:  # this is a delete
    if auth_entity:
      redirect(f'/delete/finish?auth_entity={auth_entity.key.urlsafe().decode()}&state={state}', logins=logins)
    else:
      flash(f'If you want to disable, please approve the {source_cls.GR_CLASS.NAME} prompt.')
      source_key = state_obj.get('source')
      if source_key:
        source = ndb.Key(urlsafe=source_key).get()
        if source:
          redirect(source.bridgy_url())

      redirect('/')


def construct_state_param_for_add(state=None, **kwargs):
  """Construct the state parameter if one isn't explicitly passed in.

  The following keys are common:
  - operation: 'add' or 'delete'
  - feature: 'listen', 'publish', or 'webmention'
  - callback: an optional external callback, that we will redirect to at the end of the authorization handshake
  - source: the source key, only applicable to deletes
  """
  state_obj = util.decode_oauth_state(state)
  if not state_obj:
    state_obj = {field: request.values.get(field) for field in
                 ('callback', 'feature', 'id', 'user_url')}
    state_obj['operation'] = request.values.get('operation') or 'add'

  if kwargs:
    state_obj.update(kwargs)

  return util.encode_oauth_state(state_obj)


def get_logins():
  """Extracts the current user page paths from the logins cookie.

  The logins cookie is set in :meth:`redirect` and :class:`Redirect`.

  Returns:
    list of :class:`Login` objects
  """
  logins_str = request.cookies.get('logins')
  if not logins_str:
    return []

  logins = []
  for val in set(urllib.parse.unquote_plus(logins_str).split('|')):
    parts = val.split('?', 1)
    path = parts[0]
    if not path:
      continue
    name = parts[1] if len(parts) > 1 else ''
    site, _ = path.strip('/').split('/')
    logins.append(Login(path=path, site=site, name=name))

  return logins


def preprocess_source(source):
  """Prepares a source entity for rendering in the source.html template.

  - convert image URLs to https if we're serving over SSL
  - set 'website_links' attr to list of pretty HTML links to domain_urls

  Args:
    source: :class:`models.Source` entity
  """
  if source.picture:
    source.picture = util.update_scheme(source.picture, request)
  source.website_links = [
    util.pretty_link(url, attrs={'rel': 'me', 'class': 'u-url'})
    for url in source.domain_urls]
  return source


def oauth_starter(oauth_start_view, **kwargs):
  """Returns an oauth-dropins start view that injects the state param.

  Args:
    oauth_start_view: oauth-dropins :class:`Start` to use,
      e.g. :class:`oauth_dropins.twitter.Start`.
    kwargs: passed to :meth:`construct_state_param_for_add()`
  """
  class Start(oauth_start_view):
    def redirect_url(self, state=None, **ru_kwargs):
      return super().redirect_url(
        state=construct_state_param_for_add(state, **kwargs), **ru_kwargs)

  return Start


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
