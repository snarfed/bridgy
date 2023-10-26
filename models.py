"""Datastore model classes."""
from datetime import datetime, timedelta, timezone
import logging
import os
import re

from google.cloud import ndb
from granary import as1
from granary import microformats2
from granary import source as gr_source
from oauth_dropins.indieauth import IndieAuth
from oauth_dropins.instagram import INSTAGRAM_SESSIONID_COOKIE
from oauth_dropins.webutil import webmention
from oauth_dropins.webutil.flask_util import flash
from oauth_dropins.webutil.models import StringIdModel
from oauth_dropins.webutil.util import json_dumps, json_loads
import requests

import superfeedr
import util

logger = logging.getLogger(__name__)

VERB_TYPES = ('post', 'comment', 'like', 'react', 'repost', 'rsvp', 'tag')
PUBLISH_TYPES = VERB_TYPES + ('preview', 'delete')

MAX_AUTHOR_URLS = 5

REFETCH_HFEED_TRIGGER = datetime.fromtimestamp(-1, tz=timezone.utc)

# limit size of block lists stored in source entities to try to keep whole
# entiry under 1MB datastore limit:
# https://cloud.google.com/datastore/docs/concepts/limits
BLOCKLIST_MAX_IDS = 20000

# maps string short name to Source subclass. populated by SourceMeta.
sources = {}


def get_type(obj):
  """Returns the :class:`Response` or :class:`Publish` type for an AS object."""
  type = obj.get('objectType')
  verb = obj.get('verb')
  if type == 'activity' and verb == 'share':
    return 'repost'
  elif type == 'issue':
    return 'post'
  elif verb in as1.RSVP_VERB_TO_COLLECTION:
    return 'rsvp'
  elif (type == 'comment' or obj.get('inReplyTo') or
        any(o.get('inReplyTo') for o in
            (util.get_list(obj, 'object')) + util.get_list(obj, 'context'))):
    return 'comment'
  elif verb in VERB_TYPES:
    return verb
  else:
    return 'post'


class DisableSource(Exception):
  """Raised when a user has deauthorized our app inside a given platform."""


class SourceMeta(ndb.MetaModel):
  """:class:`Source` metaclass. Registers all subclasses in the ``sources`` global."""
  def __new__(meta, name, bases, class_dict):
    cls = ndb.MetaModel.__new__(meta, name, bases, class_dict)
    if cls.SHORT_NAME:
      sources[cls.SHORT_NAME] = cls
    return cls


class Source(StringIdModel, metaclass=SourceMeta):
  """A silo account, e.g. a Facebook or Google+ account.

  Each concrete silo class should subclass this class.
  """
  STATUSES = ('enabled', 'disabled')
  POLL_STATUSES = ('ok', 'error', 'polling')
  FEATURES = ('listen', 'publish', 'webmention', 'email')

  # short name for this site type. used in URLs, etc.
  SHORT_NAME = None
  # the corresponding granary class
  GR_CLASS = None
  # oauth-dropins Start class
  OAUTH_START = None
  # oauth-dropins datastore model class
  AUTH_MODEL = None
  # whether Bridgy supports listen for this silo
  CAN_LISTEN = True
  # whether Bridgy supports publish for this silo
  CAN_PUBLISH = None
  # string name of oauth-dropins auth entity property to use as Micropub token
  MICROPUB_TOKEN_PROPERTY = None
  # whether this source should poll automatically, or only when triggered
  # (eg Instagram)
  AUTO_POLL = True
  # how often to poll for responses
  FAST_POLL = timedelta(minutes=30)
  # how often to poll sources that have never sent a webmention
  SLOW_POLL = timedelta(days=1)
  # how often to poll sources that are currently rate limited by their silo
  RATE_LIMITED_POLL = SLOW_POLL
  # how long to wait after signup for a successful webmention before dropping to
  # the lower frequency poll
  FAST_POLL_GRACE_PERIOD = timedelta(days=7)
  # how often refetch author url to look for updated syndication links
  FAST_REFETCH = timedelta(hours=6)
  # refetch less often (this often) if it's been >2w since the last synd link
  SLOW_REFETCH = timedelta(days=2)
  # rate limiting HTTP status codes returned by this silo. e.g. twitter returns
  # 429, instagram 503, google+ 403.
  RATE_LIMIT_HTTP_CODES = ('429',)
  DISABLE_HTTP_CODES = ('401',)
  TRANSIENT_ERROR_HTTP_CODES = ()
  # whether granary supports fetching block lists
  HAS_BLOCKS = False
  # whether to require a u-syndication link for backfeed
  BACKFEED_REQUIRES_SYNDICATION_LINK = False
  # ignore fragments when comparing syndication links in OPD
  IGNORE_SYNDICATION_LINK_FRAGMENTS = False
  # convert username to all lower case to use as key name
  USERNAME_KEY_ID = False

  # Maps Publish.type (e.g. 'like') to source-specific human readable type label
  # (e.g. 'favorite'). Subclasses should override this.
  TYPE_LABELS = {}

  # subclasses should override this
  URL_CANONICALIZER = util.UrlCanonicalizer()

  # Regexps for URL paths that don't accept incoming webmentions. Currently used
  # by Blogger.
  PATH_BLOCKLIST = ()

  created = ndb.DateTimeProperty(auto_now_add=True, required=True, tzinfo=timezone.utc)
  url = ndb.StringProperty()
  username = ndb.StringProperty()
  status = ndb.StringProperty(choices=STATUSES, default='enabled')
  poll_status = ndb.StringProperty(choices=POLL_STATUSES, default='ok')
  rate_limited = ndb.BooleanProperty(default=False)
  name = ndb.StringProperty()  # full human-readable name
  picture = ndb.StringProperty()
  domains = ndb.StringProperty(repeated=True)
  domain_urls = ndb.StringProperty(repeated=True)
  features = ndb.StringProperty(repeated=True, choices=FEATURES)
  superfeedr_secret = ndb.StringProperty()
  webmention_endpoint = ndb.StringProperty()

  # points to an oauth-dropins auth entity. The model class should be a subclass
  # of oauth_dropins.BaseAuth. the token should be generated with the
  # offline_access scope so that it doesn't expire.
  auth_entity = ndb.KeyProperty()

  #
  # listen-only properties
  #
  last_polled = ndb.DateTimeProperty(default=util.EPOCH, tzinfo=timezone.utc)
  last_poll_attempt = ndb.DateTimeProperty(default=util.EPOCH, tzinfo=timezone.utc)
  last_webmention_sent = ndb.DateTimeProperty(tzinfo=timezone.utc)
  last_public_post = ndb.DateTimeProperty(tzinfo=timezone.utc)
  recent_private_posts = ndb.IntegerProperty(default=0)

  # the last time we re-fetched the author's url looking for updated
  # syndication links
  last_hfeed_refetch = ndb.DateTimeProperty(default=util.EPOCH, tzinfo=timezone.utc)

  # the last time we've seen a rel=syndication link for this Source.
  # we won't spend the time to re-fetch and look for updates if there's
  # never been one
  last_syndication_url = ndb.DateTimeProperty(tzinfo=timezone.utc)
  # the last time we saw a syndication link in an h-feed, as opposed to just on
  # permalinks. background: https://github.com/snarfed/bridgy/issues/624
  last_feed_syndication_url = ndb.DateTimeProperty(tzinfo=timezone.utc)

  last_activity_id = ndb.StringProperty()
  last_activities_etag = ndb.StringProperty()
  last_activities_cache_json = ndb.TextProperty()
  seen_responses_cache_json = ndb.TextProperty(compressed=True)

  # populated in Poll.poll(), used by handlers
  blocked_ids = ndb.JsonProperty(compressed=True)

  # maps updated property names to values that put_updates() writes back to the
  # datastore transactionally. set this to {} before beginning.
  updates = None

  # gr_source is *not* set to None by default here, since it needs to be unset
  # for __getattr__ to run when it's accessed.

  def __init__(self, *args, id=None, **kwargs):
    """Constructor. Escapes the key string id if it starts with ``__``."""
    username = kwargs.get('username')
    if self.USERNAME_KEY_ID and username and not id:
      id = username.lower()
    if id and id.startswith('__'):
      id = '\\' + id
    super().__init__(*args, id=id, **kwargs)

  def key_id(self):
    """Returns the key's unescaped string id."""
    id = self.key.id()
    return id[1:] if id[0] == '\\' else id

  @classmethod
  def new(cls, **kwargs):
    """Factory method. Creates and returns a new instance for the current user.

    To be implemented by subclasses.
    """
    raise NotImplementedError()

  def __getattr__(self, name):
    """Lazily load the auth entity and instantiate :attr:`self.gr_source`.

    Once :attr:`self.gr_source` is set, this method will *not* be called;
    :attr:`gr_source` will be returned normally.
    """
    if name != 'gr_source':
      return getattr(super(), name)

    super_attr = getattr(super(), name, None)
    if super_attr:
      return super_attr
    elif not self.auth_entity:
      return None

    auth_entity = self.auth_entity.get()
    try:
      refresh_token = auth_entity.refresh_token
      self.gr_source = self.GR_CLASS(refresh_token)
      return self.gr_source
    except AttributeError:
      logger.info('no refresh_token')
    args = auth_entity.access_token()
    if not isinstance(args, tuple):
      args = (args,)

    kwargs = {}
    if self.key.kind() == 'FacebookPage' and auth_entity.type == 'user':
      kwargs = {'user_id': self.key_id()}
    elif self.key.kind() == 'Instagram':
      kwargs = {'scrape': True, 'cookie': INSTAGRAM_SESSIONID_COOKIE}
    elif self.key.kind() == 'Mastodon':
      args = (auth_entity.instance(),) + args
      inst = auth_entity.app.get().instance_info
      if inst:
        j = json_loads(inst)
        truncate_text_length = j.get("configuration", {}).get('statuses', {}).get('max_characters', None) or j.get('max_toot_chars', None)
      else:
        truncate_text_length = None
      kwargs = {
        'user_id': json_loads(auth_entity.user_json).get('id'),
        # https://docs-develop.pleroma.social/backend/API/differences_in_mastoapi_responses/#instance
        'truncate_text_length': truncate_text_length,
      }
    elif self.key.kind() == 'Twitter':
      kwargs = {'username': self.key_id()}
    elif self.key.kind() == 'Bluesky':
      args = (json_loads(auth_entity.user_json).get('handle'),)
      kwargs = {'did': auth_entity.did, 'app_password': auth_entity.password}

    self.gr_source = self.GR_CLASS(*args, **kwargs)
    return self.gr_source

  @classmethod
  def lookup(cls, id):
    """Returns the entity with the given id.

    By default, interprets id as just the key id. Subclasses may extend this to
    support usernames, etc.

    Ideally, if ``USERNAME_KEY_ID``, normalize to lower case before looking up.
    We'd need to backfill all existing entities with upper case key ids, though,
    which we're not planning to do. https://github.com/snarfed/bridgy/issues/884
    """
    if id and id.startswith('__'):
      id = '\\' + id
    return ndb.Key(cls, id).get()

  def user_tag_id(self):
    """Returns the tag URI for this source, e.g. ``tag:plus.google.com:123456``."""
    return self.gr_source.tag_uri(self.key_id())

  def bridgy_path(self):
    """Returns the Bridgy page URL path for this source."""
    return f'/{self.SHORT_NAME}/{self.key_id()}'

  def bridgy_url(self):
    """Returns the Bridgy page URL for this source."""
    return util.host_url(self.bridgy_path())

  def silo_url(self, handler):
    """Returns the silo account URL, e.g. https://twitter.com/foo."""
    raise NotImplementedError()

  def label(self):
    """Human-readable label for this source."""
    return f'{self.label_name()} ({self.GR_CLASS.NAME})'

  def label_name(self):
    """Human-readable name or username for this source, whichever is preferred."""
    return self.name or self.key_id()

  def post_id(self, url):
    """
    Resolve the ID of a post from a URL.
    By default calls out to Granary's classmethod but can be
    overridden if a URL needs user-specific treatment.
    """
    return self.gr_source.post_id(url)

  @classmethod
  @ndb.transactional()
  def put_updates(cls, source):
    """Writes ``source.updates`` to the datastore transactionally.

    Returns:
      source (Source)

    Returns:
      ``source``, updated
    """
    if not source.updates:
      return source

    to_log = {k: v for k, v in source.updates.items() if not k.endswith('_json')}
    logger.info(f'Updating {source.label()} {source.bridgy_path()} : {to_log!r}')

    updates = source.updates
    source = source.key.get()
    source.updates = updates
    for name, val in updates.items():
      setattr(source, name, val)

    source.put()
    return source

  def poll_period(self):
    """Returns the poll frequency for this source, as a :class:`datetime.timedelta`.

    Defaults to ~15m, depending on silo. If we've never sent a webmention for
    this source, or the last one we sent was over a month ago, we drop them down
    to ~1d after a week long grace period.
    """
    now = util.now()
    if self.rate_limited:
      return self.RATE_LIMITED_POLL
    elif now < self.created + self.FAST_POLL_GRACE_PERIOD:
      return self.FAST_POLL
    elif not self.last_webmention_sent:
      return self.SLOW_POLL
    elif self.last_webmention_sent > now - timedelta(days=7):
      return self.FAST_POLL
    elif self.last_webmention_sent > now - timedelta(days=30):
      return self.FAST_POLL * 10
    else:
      return self.SLOW_POLL

  def should_refetch(self):
    """Returns True if we should run OPD refetch on this source now."""
    now = util.now()
    if self.last_hfeed_refetch == REFETCH_HFEED_TRIGGER:
      return True
    elif not self.last_syndication_url:
      return False

    period = (self.FAST_REFETCH
              if self.last_syndication_url > now - timedelta(days=14)
              else self.SLOW_REFETCH)
    return self.last_poll_attempt >= self.last_hfeed_refetch + period

  @classmethod
  def bridgy_webmention_endpoint(cls, domain='brid.gy'):
    """Returns the Bridgy webmention endpoint for this source type."""
    return f'https://{domain}/webmention/{cls.SHORT_NAME}'

  def has_bridgy_webmention_endpoint(self):
    """Returns True if this source uses Bridgy's webmention endpoint."""
    return self.webmention_endpoint in (
      self.bridgy_webmention_endpoint(),
      self.bridgy_webmention_endpoint(domain='www.brid.gy'))

  def get_author_urls(self):
    """Determine the author urls for a particular source.

    In debug mode, replace test domains with localhost.

    Return:
      list of str: URLs, possibly empty
    """
    return [util.replace_test_domains_with_localhost(u) for u in self.domain_urls]

  def search_for_links(self):
    """Searches for activities with links to any of this source's web sites.

    * https://github.com/snarfed/bridgy/issues/456
    * https://github.com/snarfed/bridgy/issues/565

    Returns:
      list of dict: ActivityStreams activities
    """
    return []

  def get_activities_response(self, **kwargs):
    """Returns recent posts and embedded comments for this source.

    May be overridden by subclasses.
    """
    kwargs.setdefault('group_id', gr_source.SELF)
    resp = self.gr_source.get_activities_response(**kwargs)
    for activity in resp['items']:
      self._inject_user_urls(activity)
    return resp

  def get_activities(self, **kwargs):
    return self.get_activities_response(**kwargs)['items']

  def get_comment(self, comment_id, **kwargs):
    """Returns a comment from this source.

    Passes through to granary by default. May be overridden by subclasses.

    Args:
      comment_id (str): site-specific comment id
      kwargs: passed to :meth:`granary.source.Source.get_comment`

    Returns:
      dict: decoded ActivityStreams comment object, or None
    """
    comment = self.gr_source.get_comment(comment_id, **kwargs)
    if comment:
      self._inject_user_urls(comment)
    return comment

  def get_like(self, activity_user_id, activity_id, like_user_id, **kwargs):
    """Returns an ActivityStreams ``like`` activity object.

    Passes through to granary by default. May be overridden by subclasses.

    Args:
      activity_user_id (str): id of the user who posted the original activity
      activity_id (str): activity id
      like_user_id (str): id of the user who liked the activity
      kwargs: passed to :meth:`granary.source.Source.get_comment`
    """
    return self.gr_source.get_like(activity_user_id, activity_id, like_user_id,
                                   **kwargs)

  def _inject_user_urls(self, activity):
    """Adds this user's web site URLs to their user mentions (in tags), in place."""
    obj = activity.get('object') or activity
    user_tag_id = self.user_tag_id()
    for tag in obj.get('tags', []):
      if tag.get('id') == user_tag_id:
        tag.setdefault('urls', []).extend([{'value': u} for u in self.domain_urls])

  def create_comment(self, post_url, author_name, author_url, content):
    """Creates a new comment in the source silo.

    Must be implemented by subclasses.

    Args:
      post_url (str)
      author_name (str)
      author_url (str)
      content (str)

    Returns:
      dict: response with at least ``id`` field
    """
    raise NotImplementedError()

  def feed_url(self):
    """Returns the RSS or Atom (or similar) feed URL for this source.

    Must be implemented by subclasses. Currently only implemented by
    :mod:`blogger`, :mod:`medium`, :mod:`tumblr`, and :mod:`wordpress_rest`.

    Returns:
      str: URL
    """
    raise NotImplementedError()

  def edit_template_url(self):
    """Returns the URL for editing this blog's template HTML.

    Must be implemented by subclasses. Currently only implemented by
    :mod:`blogger`, :mod:`medium`, :mod:`tumblr`, and :mod:`wordpress_rest`.

    Returns:
      str: URL
    """
    raise NotImplementedError()

  def format_for_source_url(self, id):
    """Returns the given id formatted for a URL if necessary.
    Some silos use keys containing slashes.
    By default this is a no-op - can be overridden by subclasses.

    Args:
      id: The id to format

    Returns:
      string formatted id
    """
    return id

  @classmethod
  def button_html(cls, feature, **kwargs):
    """Returns an HTML string with a login form and button for this site.

    Mostly just passes through to
    :meth:`oauth_dropins.handlers.Start.button_html`.

    Returns:
      str: HTML
    """
    assert set(feature.split(',')) <= set(cls.FEATURES)
    form_extra = (kwargs.pop('form_extra', '') +
                  f'<input name="feature" type="hidden" value="{feature}" />')

    source = kwargs.pop('source', None)
    if source:
      form_extra += f'\n<input name="id" type="hidden" value="{source.key_id()}" />'

    if cls.OAUTH_START:
      return cls.OAUTH_START.button_html(
        f'/{cls.SHORT_NAME}/start',
        form_extra=form_extra,
        image_prefix='/oauth_dropins_static/',
        **kwargs)

    return ''

  @classmethod
  @ndb.transactional()
  def create_new(cls, user_url=None, **kwargs):
    """Creates and saves a new :class:`Source` and adds a poll task for it.

    Args:
      user_url (str): if provided, supersedes other urls when determining the
        ``author_url``
      kwargs: passed to :meth:`new()`

    Returns:
      Source: newly created entity
    """
    source = cls.new(**kwargs)
    if source is None:
      return None

    if not source.domain_urls:  # defer to the source if it already set this
      auth_entity = kwargs.get('auth_entity')
      if auth_entity and hasattr(auth_entity, 'user_json'):
        source.domain_urls, source.domains = source.urls_and_domains(
          auth_entity, user_url)
    logger.debug(f'URLs/domains: {source.domain_urls} {source.domains}')

    # check if this source already exists
    existing = source.key.get()
    if existing:
      # merge some fields
      source.features = set(source.features + existing.features)
      source.populate(**existing.to_dict(include=(
            'created', 'last_hfeed_refetch', 'last_poll_attempt', 'last_polled',
            'last_syndication_url', 'last_webmention_sent', 'superfeedr_secret',
            'webmention_endpoint')))
      verb = 'Updated'
    else:
      verb = 'Added'

    author_urls = source.get_author_urls()
    link = ('http://indiewebify.me/send-webmentions/?url=' + author_urls[0]
            if author_urls else 'http://indiewebify.me/#send-webmentions')
    feature = source.features[0] if source.features else 'listen'
    blurb = '%s %s. %s' % (
      verb, source.label(),
      'Try previewing a post from your web site!' if feature == 'publish'
      else '<a href="%s">Try a webmention!</a>' % link if feature == 'webmention'
      else "Refresh in a minute to see what we've found!")
    logger.info(f'{blurb} {source.bridgy_url()}')

    source.verify()
    if source.verified():
      flash(blurb)

    source.put()

    if 'webmention' in source.features:
      try:
        superfeedr.subscribe(source)
      except BaseException as e:
        code, _ = util.interpret_http_exception(e)
        if (code in superfeedr.TRANSIENT_ERROR_HTTP_CODES or
            util.is_connection_failure(e)):
          flash('Apologies, <a href="https://superfeedr.com/">Superfeedr</a> is having technical difficulties. Please try again later!')
          return None
        raise

    if 'listen' in source.features and source.AUTO_POLL:
      util.add_poll_task(source, now=True)
      util.add_poll_task(source)

    return source

  def verified(self):
    """Returns True if this source is ready to be used, False otherwise.

    See :meth:`verify()` for details. May be overridden by subclasses, e.g.
    :class:`tumblr.Tumblr`.
    """
    if not self.domains or not self.domain_urls:
      return False
    if 'webmention' in self.features and not self.webmention_endpoint:
      return False
    if ('listen' in self.features and
        not (self.webmention_endpoint or self.last_webmention_sent)):
      return False
    return True

  def verify(self, force=False):
    """Checks that this source is ready to be used.

    For blog and listen sources, this fetches their front page HTML and
    discovers their webmention endpoint. For publish sources, this checks that
    they have a domain.

    May be overridden by subclasses, e.g. :class:`tumblr.Tumblr`.

    Args:
      force (bool): if True, fully verifies (e.g. re-fetches the blog's HTML and
        performs webmention discovery) even we already think this source is
        verified.
    """
    author_urls = [u for u, d in zip(self.get_author_urls(), self.domains)
                   if not util.in_webmention_blocklist(d)]
    if ((self.verified() and not force) or self.status == 'disabled' or
        not self.features or not author_urls):
      return

    author_url = author_urls[0]
    try:
      got = webmention.discover(author_url, timeout=util.HTTP_TIMEOUT)
      self.webmention_endpoint = got.endpoint
      self._fetched_html = got.response.text
    except BaseException as e:
      logger.info('Error discovering webmention endpoint', exc_info=e)
      self.webmention_endpoint = None

    self.put()

  def urls_and_domains(self, auth_entity, user_url, actor=None,
                       resolve_source_domain=True):
    """Returns this user's valid (not webmention-blocklisted) URLs and domains.

    Converts the auth entity's ``user_json`` to an ActivityStreams actor and
    uses its ``urls`` and ``url`` fields. May be overridden by subclasses.

    Args:
      auth_entity (oauth_dropins.models.BaseAuth)
      user_url (str): optional URL passed in when authorizing
      actor (dict): optional AS actor for the user. If provided, overrides
        auth_entity
      resolve_source_domain (bool): whether to follow redirects on URLs on
        this source's domain

    Returns:
      ([str url, ...], [str domain, ...]) tuple:
    """
    if not actor:
      actor = self.gr_source.user_to_actor(json_loads(auth_entity.user_json))
    logger.debug(f'Extracting URLs and domains from actor: {json_dumps(actor, indent=2)}')

    candidates = util.trim_nulls(util.uniquify(
        [user_url] + as1.object_urls(actor)))

    if len(candidates) > MAX_AUTHOR_URLS:
      logger.info(f'Too many profile links! Only resolving the first {MAX_AUTHOR_URLS}: {candidates}')

    urls = []
    for i, url in enumerate(candidates):
      on_source_domain = util.domain_from_link(url) == self.gr_source.DOMAIN
      resolve = ((resolve_source_domain or not on_source_domain) and
                 i < MAX_AUTHOR_URLS)
      resolved = self.resolve_profile_url(url, resolve=resolve)
      if resolved:
        urls.append(resolved)

    final_urls = []
    domains = []
    for url in util.dedupe_urls(urls):  # normalizes domains to lower case
      # skip links on this source's domain itself. only currently needed for
      # Mastodon; the other silo domains are in the webmention blocklist.
      domain = util.domain_from_link(url)
      if domain != self.gr_source.DOMAIN:
        final_urls.append(url)
        domains.append(domain)

    return final_urls, domains

  @staticmethod
  def resolve_profile_url(url, resolve=True):
    """Resolves a profile URL to be added to a source.

    Args:
      url (str)
      resolve (bool): whether to make HTTP requests to follow redirects, etc.

    Returns:
      str: resolved URL, or None
    """
    final, _, ok = util.get_webmention_target(url, resolve=resolve)
    if not ok:
      return None

    final = final.lower()
    if util.schemeless(final).startswith(util.schemeless(url.lower())):
      # redirected to a deeper path. use the original higher level URL. #652
      final = url

    # If final has a path segment check if root has a matching rel=me.
    match = re.match(r'^(https?://[^/]+)/.+', final)
    if match and resolve:
      root = match.group(1)
      try:
        mf2 = util.fetch_mf2(root)
        me_urls = mf2['rels'].get('me', [])
        if final in me_urls:
          final = root
      except requests.RequestException:
        logger.warning(f"Couldn't fetch {root}, preserving path in {final}", exc_info=True)

    return final

  def canonicalize_url(self, url, activity=None, **kwargs):
    """Canonicalizes a post or object URL.

    Wraps :class:`oauth_dropins.webutil.util.UrlCanonicalizer`.
    """
    return self.URL_CANONICALIZER(url, **kwargs) if self.URL_CANONICALIZER else url

  def infer_profile_url(self, url):
    """Given a silo profile, tries to find the matching Bridgy user URL.

    Queries Bridgy's registered accounts for users with a particular
    domain in their silo profile.

    Args:
      url (str): a person's URL

    Return:
      str: URL for their profile on this service, or None

    """
    domain = util.domain_from_link(url)
    if domain == self.gr_source.DOMAIN:
      return url
    user = self.__class__.query(self.__class__.domains == domain).get()
    if user:
      return self.gr_source.user_url(user.key_id())

  def preprocess_for_publish(self, obj):
    """Preprocess an object before trying to publish it.

    By default this tries to massage person tags so that the tag's
    ``url`` points to the person's profile on this service (as opposed
    to a person's homepage).

    The object is modified in place.

    Args:
      obj (dict): ActivityStreams activity or object
    """
    if isinstance(obj, str):
      return obj

    for tag in obj.get('tags', []):
      if tag.get('objectType') == 'person':
        silo_url = None
        for url in as1.object_urls(tag):
          silo_url = url and self.infer_profile_url(url)
          if silo_url:
            break
        if silo_url:
          tag['url'] = silo_url

    # recurse on contained object(s)
    for obj in util.get_list(obj, 'object'):
      self.preprocess_for_publish(obj)

  def on_new_syndicated_post(self, syndpost):
    """Called when a new :class:`SyndicatedPost` is stored for this source.

    Args:
      syndpost (SyndicatedPost)
    """
    pass

  def is_private(self):
    """Returns True if this source is private aka protected.

    ...ie their posts are not public.
    """
    return False

  def is_beta_user(self):
    """Returns True if this is a "beta" user opted into new features.

    Beta users come from ``beta_users.txt``.
    """
    return self.bridgy_path() in util.BETA_USER_PATHS

  def load_blocklist(self):
    """Fetches this user's blocklist, if supported, and stores it in the entity."""
    if not self.HAS_BLOCKS:
      return

    try:
      ids = self.gr_source.get_blocklist_ids()
    except gr_source.RateLimited as e:
      ids = e.partial or []

    self.blocked_ids = ids[:BLOCKLIST_MAX_IDS]
    self.put()

  def is_blocked(self, obj):
    """Returns True if an object's author is being blocked.

    ...ie they're in this user's block list.

    Note that this method is tested in test_twitter.py, not test_models.py, for
    historical reasons.
    """
    if not self.blocked_ids:
      return False

    for o in [obj] + util.get_list(obj, 'object'):
      for field in 'author', 'actor':
        if o.get(field, {}).get('numeric_id') in self.blocked_ids:
          return True


class Webmentions(StringIdModel):
  """A bundle of links to send webmentions for.

  Use the :class:`Response` and :class:`BlogPost` concrete subclasses below.
  """
  STATUSES = ('new', 'processing', 'complete', 'error')

  source = ndb.KeyProperty()
  status = ndb.StringProperty(choices=STATUSES, default='new')
  leased_until = ndb.DateTimeProperty(tzinfo=timezone.utc)
  created = ndb.DateTimeProperty(auto_now_add=True, tzinfo=timezone.utc)
  updated = ndb.DateTimeProperty(auto_now=True, tzinfo=timezone.utc)

  # Original post links, ie webmention targets
  sent = ndb.StringProperty(repeated=True)
  unsent = ndb.StringProperty(repeated=True)
  error = ndb.StringProperty(repeated=True)
  failed = ndb.StringProperty(repeated=True)
  skipped = ndb.StringProperty(repeated=True)

  def label(self):
    """Returns a human-readable string description for use in log messages.

    To be implemented by subclasses.
    """
    raise NotImplementedError()

  def add_task(self):
    """Adds a propagate task for this entity.

    To be implemented by subclasses.
    """
    raise NotImplementedError()

  @ndb.transactional()
  def get_or_save(self):
    entity = existing = self.key.get()

    propagate = False
    if entity:
      # merge targets
      urls = set(entity.sent + entity.unsent + entity.error +
                 entity.failed + entity.skipped)
      for field in ('sent', 'unsent', 'error', 'failed', 'skipped'):
        entity_urls = getattr(entity, field)
        new_urls = set(getattr(self, field)) - urls
        entity_urls += new_urls
        if new_urls and field in ('unsent', 'error'):
          propagate = True
    else:
      entity = self
      propagate = self.unsent or self.error

    if propagate:
      logger.debug(f'New webmentions to propagate! {entity.label()}')
      entity.add_task()
    elif not existing:
      entity.status = 'complete'

    entity.put()
    return entity

  def restart(self):
    """Moves status and targets to 'new' and adds a propagate task."""
    self.status = 'new'
    self.unsent = util.dedupe_urls(self.unsent + self.sent + self.error +
                                   self.failed + self.skipped)
    self.sent = self.error = self.failed = self.skipped = []

    # clear any cached webmention endpoints
    with util.webmention_endpoint_cache_lock:
      for url in self.unsent:
        util.webmention_endpoint_cache.pop(util.webmention_endpoint_cache_key(url), None)

    # this datastore put and task add should be transactional, but Cloud Tasks
    # doesn't support that :(
    # https://cloud.google.com/appengine/docs/standard/python/taskqueue/push/migrating-push-queues#features-not-available
    # https://github.com/googleapis/python-tasks/issues/26
    #
    # The new "bundled services" bridge for the old App Engine APIs still
    # supports them, but only because that's literally on the old backends,
    # which seems like a dead end.
    # https://groups.google.com/g/google-appengine/c/22BKInlWty0/m/05ObNEdsAgAJ
    self.put()
    self.add_task()


class Response(Webmentions):
  """A comment, like, or repost to be propagated.

  The key name is the comment object id as a tag URI.
  """
  # ActivityStreams JSON activity and comment, like, or repost
  type = ndb.StringProperty(choices=VERB_TYPES, default='comment')
  # These are TextProperty, and not JsonProperty, so that their plain text is
  # visible in the App Engine admin console. (JsonProperty uses a blob. :/)
  activities_json = ndb.TextProperty(repeated=True)
  response_json = ndb.TextProperty()
  # Old values for response_json. Populated when the silo reports that the
  # response has changed, e.g. the user edited a comment or changed their RSVP
  # to an event. Currently unused, kept for historical records only.
  old_response_jsons = ndb.TextProperty(repeated=True)
  # JSON dict mapping original post url to activity index in activities_json.
  # only set when there's more than one activity.
  urls_to_activity = ndb.TextProperty()
  # Original post links found by original post discovery
  original_posts = ndb.StringProperty(repeated=True)

  def label(self):
    return ' '.join((self.key.kind(), self.type, self.key.id(),
                     json_loads(self.response_json).get('url', '[no url]')))

  def add_task(self):
    util.add_propagate_task(self)

  @staticmethod
  def get_type(obj):
    type = get_type(obj)
    return type if type in VERB_TYPES else 'comment'

  def get_or_save(self, source, restart=False):
    resp = super().get_or_save()

    if (self.type != resp.type or
        as1.activity_changed(json_loads(resp.response_json),
                             json_loads(self.response_json),
                             log=True)):
      logger.info(f'Response changed! Re-propagating. Original: {resp}')

      resp.old_response_jsons = [resp.response_json] + resp.old_response_jsons[:10]

      response_json_to_append = json_loads(self.response_json)
      as1.append_in_reply_to(json_loads(resp.response_json), response_json_to_append)
      self.response_json = json_dumps(util.trim_nulls(response_json_to_append))
      resp.response_json = self.response_json
      resp.restart(source)
    elif restart and resp is not self:  # ie it already existed
      resp.restart(source)

    return resp

  def restart(self, source=None):
    """Moves status and targets to 'new' and adds a propagate task."""
    # add original posts with syndication URLs
    # TODO: unify with Poll.repropagate_old_responses()
    if not source:
      source = self.source.get()

    synd_urls = set()
    for activity_json in self.activities_json:
      activity = json_loads(activity_json)
      url = activity.get('url') or activity.get('object', {}).get('url')
      if url:
        url = source.canonicalize_url(url, activity=activity)
        if url:
          synd_urls.add(url)

    if synd_urls:
      self.unsent += [synd.original for synd in
                      SyndicatedPost.query(SyndicatedPost.syndication.IN(synd_urls))
                      if synd.original]

    return super().restart()


class Activity(StringIdModel):
  """An activity with responses to be propagated.

  The key name is the activity id as a tag URI.

  Currently only used for posts sent to us by the browser extension.
  """
  source = ndb.KeyProperty()
  created = ndb.DateTimeProperty(auto_now_add=True, tzinfo=timezone.utc)
  updated = ndb.DateTimeProperty(auto_now=True, tzinfo=timezone.utc)
  activity_json = ndb.TextProperty()
  html = ndb.TextProperty()


class BlogPost(Webmentions):
  """A blog post to be processed for links to send webmentions to.

  The key name is the URL.
  """
  feed_item = ndb.JsonProperty(compressed=True)  # from Superfeedr

  def label(self):
    url = self.feed_item.get('permalinkUrl') if self.feed_item else None
    return ' '.join((self.key.kind(), self.key.id(), url or '[no url]'))

  def add_task(self):
    util.add_propagate_blogpost_task(self)


class PublishedPage(StringIdModel):
  """Minimal root entity for :class:`Publish` children with the same source URL.

  Key id is the string source URL.
  """
  pass


class Publish(ndb.Model):
  """A comment, like, repost, or RSVP published into a silo.

  Child of a :class:`PublishedPage` entity.
  """
  STATUSES = ('new', 'complete', 'failed', 'deleted')

  type = ndb.StringProperty(choices=PUBLISH_TYPES)
  status = ndb.StringProperty(choices=STATUSES, default='new')
  source = ndb.KeyProperty()
  html = ndb.TextProperty()  # raw HTML fetched from source
  mf2 = ndb.JsonProperty()   # mf2 from micropub request
  published = ndb.JsonProperty(compressed=True)
  created = ndb.DateTimeProperty(auto_now_add=True, tzinfo=timezone.utc)
  updated = ndb.DateTimeProperty(auto_now=True, tzinfo=timezone.utc)

  def type_label(self):
    """Returns silo-specific string type, e.g. 'favorite' instead of 'like'."""
    for cls in sources.values():  # global
      if cls.__name__ == self.source.kind():
        return cls.TYPE_LABELS.get(self.type, self.type)

    return self.type


class BlogWebmention(Publish, StringIdModel):
  """Datastore entity for webmentions for hosted blog providers.

  Key id is the source URL and target URL concated with a space, ie ``SOURCE
  TARGET``. The source URL is *always* the URL given in the webmention HTTP
  request. If the source page has a ``u-url``, that's stored in the
  :attr:`u_url` property. The target URL is always the final URL, after any
  redirects.

  Reuses :class:`Publish`'s fields, but otherwise unrelated.
  """
  # If the source page has a u-url, it's stored here and overrides the source
  # URL in the key id.
  u_url = ndb.StringProperty()

  # Any initial target URLs that redirected to the final target URL, in redirect
  # order.
  redirected_target_urls = ndb.StringProperty(repeated=True)

  def source_url(self):
    return self.u_url or self.key.id().split()[0]

  def target_url(self):
    return self.key.id().split()[1]


class SyndicatedPost(ndb.Model):
  """Represents a syndicated post and its discovered original (or not
  if we found no original post).  We discover the relationship by
  following rel=syndication links on the author's h-feed.

  See :mod:`original_post_discovery`.

  When a :class:`SyndicatedPost` entity is about to be stored,
  :meth:`source.Source.on_new_syndicated_post` is called before it's stored.
  """

  syndication = ndb.StringProperty()
  original = ndb.StringProperty()
  created = ndb.DateTimeProperty(auto_now_add=True, tzinfo=timezone.utc)
  updated = ndb.DateTimeProperty(auto_now=True, tzinfo=timezone.utc)

  @classmethod
  @ndb.transactional()
  def insert_original_blank(cls, source, original):
    """Insert a new original -> None relationship. Does a check-and-set to
    make sure no previous relationship exists for this original. If
    there is, nothing will be added.

    Args:
      source (Source)
      original (str)
    """
    if cls.query(cls.original == original, ancestor=source.key).get():
      return
    cls(parent=source.key, original=original, syndication=None).put()

  @classmethod
  @ndb.transactional()
  def insert_syndication_blank(cls, source, syndication):
    """Insert a new syndication -> None relationship. Does a check-and-set
    to make sure no previous relationship exists for this
    syndication. If there is, nothing will be added.

    Args:
      source (Source)
      original (str)
    """

    if cls.query(cls.syndication == syndication, ancestor=source.key).get():
      return
    cls(parent=source.key, original=None, syndication=syndication).put()

  @classmethod
  @ndb.transactional()
  def insert(cls, source, syndication, original):
    """Insert a new (non-blank) syndication -> original relationship.

    This method does a check-and-set within transaction to avoid
    including duplicate relationships.

    If blank entries exists for the syndication or original URL
    (i.e. syndication -> None or original -> None), they will first be
    removed. If non-blank relationships exist, they will be retained.

    Args:
      source (Source)
      syndication (str)
      original (str)

    Returns:
      SyndicatedPost: newly created or preexisting entity
    """
    # check for an exact match
    duplicate = cls.query(cls.syndication == syndication,
                          cls.original == original,
                          ancestor=source.key).get()
    if duplicate:
      return duplicate

    # delete blanks (expect at most 1 of each)
    for filter in (ndb.AND(cls.syndication == syndication, cls.original == None),
                   ndb.AND(cls.original == original, cls.syndication == None)):
      for synd in cls.query(filter, ancestor=source.key).fetch(keys_only=True):
        synd.delete()

    r = cls(parent=source.key, original=original, syndication=syndication)
    r.put()
    return r


class Domain(StringIdModel):
  """A domain owned by a user.

  Ownership is proven via IndieAuth. Supports secret tokens associated with each
  domain. Clients can include a token with requests that operate on a given
  domain, eg sending posts and responses from the browser extension.

  Key id is the string domain, eg ``example.com``.
  """
  tokens = ndb.StringProperty(repeated=True)
  auth = ndb.KeyProperty(IndieAuth)
  created = ndb.DateTimeProperty(auto_now_add=True, tzinfo=timezone.utc)
  updated = ndb.DateTimeProperty(auto_now=True, tzinfo=timezone.utc)
