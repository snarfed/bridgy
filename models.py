"""Datastore model classes.
"""
from __future__ import unicode_literals

from builtins import zip
import datetime
import logging
import re

import appengine_config
from appengine_config import HTTP_TIMEOUT

from granary import microformats2
from granary import source as gr_source
from oauth_dropins.webutil.models import StringIdModel
from oauth_dropins.webutil.util import json_dumps, json_loads
import requests
from webmentiontools import send

import superfeedr
import util

from google.appengine.ext import ndb
from future.utils import with_metaclass

VERB_TYPES = ('post', 'comment', 'like', 'react', 'repost', 'rsvp', 'tag')
PUBLISH_TYPES = VERB_TYPES + ('preview', 'delete')

MAX_AUTHOR_URLS = 5

REFETCH_HFEED_TRIGGER = datetime.datetime.utcfromtimestamp(-1)

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
  elif verb in gr_source.RSVP_VERB_TO_COLLECTION:
    return 'rsvp'
  elif (type == 'comment' or obj.get('inReplyTo') or
        obj.get('context', {}).get('inReplyTo')):
    return 'comment'
  elif verb in VERB_TYPES:
    return verb
  else:
    return 'post'


class DisableSource(Exception):
  """Raised when a user has deauthorized our app inside a given platform."""


class SourceMeta(ndb.MetaModel):
  """:class:`Source` metaclass. Registers all subclasses in the sources global."""
  def __new__(meta, name, bases, class_dict):
    cls = ndb.MetaModel.__new__(meta, name, bases, class_dict)
    sources[cls.SHORT_NAME] = cls
    return cls


class Source(with_metaclass(SourceMeta, StringIdModel)):
  """A silo account, e.g. a Facebook or Google+ account.

  Each concrete silo class should subclass this class.
  """

  # Turn off NDB instance and memcache caching.
  # https://developers.google.com/appengine/docs/python/ndb/cache
  # https://github.com/snarfed/bridgy/issues/558
  # https://github.com/snarfed/bridgy/issues/68
  _use_cache = False

  STATUSES = ('enabled', 'disabled', 'error')  # 'error' is deprecated
  POLL_STATUSES = ('ok', 'error', 'polling')
  FEATURES = ('listen', 'publish', 'webmention', 'email')

  # short name for this site type. used in URLs, etc.
  SHORT_NAME = None
  # the corresponding granary class
  GR_CLASS = None
  # oauth-dropins StartHandler class
  OAUTH_START_HANDLER = None
  # whether Bridgy supports publish for this silo
  CAN_PUBLISH = None
  # how often to poll for responses
  FAST_POLL = datetime.timedelta(minutes=30)
  # how often to poll sources that have never sent a webmention
  SLOW_POLL = datetime.timedelta(days=1)
  # how often to poll sources that are currently rate limited by their silo
  RATE_LIMITED_POLL = SLOW_POLL
  # how long to wait after signup for a successful webmention before dropping to
  # the lower frequency poll
  FAST_POLL_GRACE_PERIOD = datetime.timedelta(days=7)
  # how often refetch author url to look for updated syndication links
  FAST_REFETCH = datetime.timedelta(hours=6)
  # refetch less often (this often) if it's been >2w since the last synd link
  SLOW_REFETCH = datetime.timedelta(days=2)
  # rate limiting HTTP status codes returned by this silo. e.g. twitter returns
  # 429, instagram 503, google+ 403.
  # TODO: facebook. it returns 200 and reports the error in the response.
  # https://developers.facebook.com/docs/reference/ads-api/api-rate-limiting/
  RATE_LIMIT_HTTP_CODES = ('429',)
  DISABLE_HTTP_CODES = ('401',)
  TRANSIENT_ERROR_HTTP_CODES = ()
  # whether granary supports fetching block lists
  HAS_BLOCKS = False
  # whether to require a u-syndication link for backfeed
  BACKFEED_REQUIRES_SYNDICATION_LINK = False

  # Maps Publish.type (e.g. 'like') to source-specific human readable type label
  # (e.g. 'favorite'). Subclasses should override this.
  TYPE_LABELS = {}

  # subclasses should override this
  URL_CANONICALIZER = util.UrlCanonicalizer(headers=util.REQUEST_HEADERS)

  # Regexps for URL paths that don't accept incoming webmentions. Currently used
  # by Blogger.
  PATH_BLACKLIST = ()

  created = ndb.DateTimeProperty(auto_now_add=True, required=True)
  url = ndb.StringProperty()
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
  # of oauth_dropins.BaseAuth.
  # the token should be generated with the offline_access scope so that it
  # doesn't expire. details: http://developers.facebook.com/docs/authentication/
  auth_entity = ndb.KeyProperty()

  #
  # listen-only properties
  #
  last_polled = ndb.DateTimeProperty(default=util.EPOCH)
  last_poll_attempt = ndb.DateTimeProperty(default=util.EPOCH)
  last_webmention_sent = ndb.DateTimeProperty()
  last_public_post = ndb.DateTimeProperty()
  recent_private_posts = ndb.IntegerProperty()

  # the last time we re-fetched the author's url looking for updated
  # syndication links
  last_hfeed_refetch = ndb.DateTimeProperty(default=util.EPOCH)

  # the last time we've seen a rel=syndication link for this Source.
  # we won't spend the time to re-fetch and look for updates if there's
  # never been one
  last_syndication_url = ndb.DateTimeProperty()
  # the last time we saw a syndication link in an h-feed, as opposed to just on
  # permalinks. background: https://github.com/snarfed/bridgy/issues/624
  last_feed_syndication_url = ndb.DateTimeProperty()

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

  @classmethod
  def new(cls, handler, **kwargs):
    """Factory method. Creates and returns a new instance for the current user.

    To be implemented by subclasses.
    """
    raise NotImplementedError()

  def __getattr__(self, name):
    """Lazily load the auth entity and instantiate :attr:`self.gr_source`.

    Once :attr:`self.gr_source` is set, this method will *not* be called;
    :attr:`gr_source` will be returned normally.
    """
    if name == 'gr_source' and self.auth_entity:
      auth_entity = self.auth_entity.get()
      args = auth_entity.access_token()
      if not isinstance(args, tuple):
        args = (args,)

      kwargs = {}
      if self.key.kind() == 'FacebookPage' and auth_entity.type == 'user':
        kwargs = {'user_id': self.key.id()}
      elif self.key.kind() == 'Instagram':
        kwargs = {'scrape': True, 'cookie': appengine_config.INSTAGRAM_SESSIONID_COOKIE}
      elif self.key.kind() == 'Mastodon':
        args = (auth_entity.instance(),) + args
        kwargs = {'user_id': json_loads(auth_entity.user_json).get('id')}
      elif self.key.kind() == 'Twitter':
        kwargs = {'username': self.key.id()}

      self.gr_source = self.GR_CLASS(*args, **kwargs)
      return self.gr_source

    if name == 'gr_source' and self.key.kind() == 'FacebookEmailAccount':
      from granary import facebook as gr_facebook
      self.gr_source = gr_facebook.Facebook(user_id=self.key.id())
      return self.gr_source

    return getattr(super(Source, self), name)

  @classmethod
  def lookup(cls, id):
    """Returns the entity with the given id.

    By default, interprets id as just the key id. Subclasses may extend this to
    support usernames, etc.
    """
    return ndb.Key(cls, id).get()

  def user_tag_id(self):
    """Returns the tag URI for this source, e.g. 'tag:plus.google.com:123456'."""
    return self.gr_source.tag_uri(self.key.id())

  def bridgy_path(self):
    """Returns the Bridgy page URL path for this source."""
    return '/%s/%s' % (self.SHORT_NAME, self.key.string_id())

  def bridgy_url(self, handler):
    """Returns the Bridgy page URL for this source."""
    return util.host_url(handler) + self.bridgy_path()

  def silo_url(self, handler):
    """Returns the silo account URL, e.g. https://twitter.com/foo."""
    raise NotImplementedError()

  def label(self):
    """Human-readable label for this source."""
    return '%s (%s)' % (self.label_name(), self.GR_CLASS.NAME)

  def label_name(self):
    """Human-readable name or username for this source, whichever is preferred."""
    return self.name

  @classmethod
  @ndb.transactional
  def put_updates(cls, source):
    """Writes source.updates to the datastore transactionally.

    Returns:
      source: :class:`Source`

    Returns:
      the updated :class:`Source`
    """
    if not source.updates:
      return source

    logging.info('Updating %s %s : %r', source.label(), source.bridgy_path(),
                 {k: v for k, v in source.updates.items() if not k.endswith('_json')})

    updates = source.updates
    source = source.key.get()
    source.updates = updates  # because FacebookPage._pre_put_hook uses it
    for name, val in updates.items():
      setattr(source, name, val)

    if source.status == 'error':  # deprecated
      logging.warning('Resetting status from error to enabled')
      source.status = 'enabled'

    source.put()
    return source

  def poll_period(self):
    """Returns the poll frequency for this source, as a :class:`datetime.timedelta`.

    Defaults to ~15m, depending on silo. If we've never sent a webmention for
    this source, or the last one we sent was over a month ago, we drop them down
    to ~1d after a week long grace period.
    """
    now = datetime.datetime.now()
    if self.rate_limited:
      return self.RATE_LIMITED_POLL
    elif now < self.created + self.FAST_POLL_GRACE_PERIOD:
      return self.FAST_POLL
    elif not self.last_webmention_sent:
      return self.SLOW_POLL
    elif self.last_webmention_sent > now - datetime.timedelta(days=7):
      return self.FAST_POLL
    elif self.last_webmention_sent > now - datetime.timedelta(days=30):
      return self.FAST_POLL * 10
    else:
      return self.SLOW_POLL

  def should_refetch(self):
    """Returns True if we should run OPD refetch on this source now."""
    now = datetime.datetime.now()
    if self.last_hfeed_refetch == REFETCH_HFEED_TRIGGER:
      return True
    elif not self.last_syndication_url:
      return False

    period = (self.FAST_REFETCH
              if self.last_syndication_url > now - datetime.timedelta(days=14)
              else self.SLOW_REFETCH)
    return self.last_poll_attempt >= self.last_hfeed_refetch + period

  @classmethod
  def bridgy_webmention_endpoint(cls, domain='brid.gy'):
    """Returns the Bridgy webmention endpoint for this source type."""
    return 'https://%s/webmention/%s' % (domain, cls.SHORT_NAME)

  def has_bridgy_webmention_endpoint(self):
    """Returns True if this source uses Bridgy's webmention endpoint."""
    return self.webmention_endpoint in (
      self.bridgy_webmention_endpoint(),
      self.bridgy_webmention_endpoint(domain='www.brid.gy'))

  def get_author_urls(self):
    """Determine the author urls for a particular source.

    In debug mode, replace test domains with localhost.

    Return:
      a list of string URLs, possibly empty
    """
    return [util.replace_test_domains_with_localhost(u) for u in self.domain_urls]

  def search_for_links(self):
    """Searches for activities with links to any of this source's web sites.

    https://github.com/snarfed/bridgy/issues/456
    https://github.com/snarfed/bridgy/issues/565

    Returns:
      sequence of ActivityStreams activity dicts
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
      comment_id: string, site-specific comment id
      kwargs: passed to :meth:`granary.source.Source.get_comment`

    Returns:
      dict, decoded ActivityStreams comment object, or None
    """
    comment = self.gr_source.get_comment(comment_id, **kwargs)
    if comment:
      self._inject_user_urls(comment)
    return comment

  def get_like(self, activity_user_id, activity_id, like_user_id, **kwargs):
    """Returns an ActivityStreams 'like' activity object.

    Passes through to granary by default. May be overridden
    by subclasses.

    Args:
      activity_user_id: string id of the user who posted the original activity
      activity_id: string activity id
      like_user_id: string id of the user who liked the activity
      kwargs: passed to granary.Source.get_comment
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
      post_url: string
      author_name: string
      author_url: string
      content: string

    Returns:
      response dict with at least 'id' field
    """
    raise NotImplementedError()

  def feed_url(self):
    """Returns the RSS or Atom (or similar) feed URL for this source.

    Must be implemented by subclasses. Currently only implemented by
    :mod:`blogger`, :mod:`medium`, :mod:`tumblr`, and :mod:`wordpress_rest`.

    Returns:
      string URL
    """
    raise NotImplementedError()

  def edit_template_url(self):
    """Returns the URL for editing this blog's template HTML.

    Must be implemented by subclasses. Currently only implemented by
    :mod:`blogger`, :mod:`medium`, :mod:`tumblr`, and :mod:`wordpress_rest`.

    Returns:
      string URL
    """
    raise NotImplementedError()

  @classmethod
  def button_html(cls, feature, **kwargs):
    """Returns an HTML string with a login form and button for this site.

    Mostly just passes through to
    :meth:`oauth_dropins.handlers.StartHandler.button_html`.

    Returns: string, HTML
    """
    assert feature in cls.FEATURES
    form_extra = (kwargs.pop('form_extra', '') +
                  '<input name="feature" type="hidden" value="%s" />' % feature)

    source = kwargs.pop('source', None)
    if source:
      form_extra += ('\n<input name="id" type="hidden" value="%s" />' %
                     source.key.id())

    return cls.OAUTH_START_HANDLER.button_html(
      '/%s/start' % cls.SHORT_NAME,
      form_extra=form_extra,
      image_prefix='/oauth_dropins/static/',
      **kwargs)

  @classmethod
  def create_new(cls, handler, user_url=None, **kwargs):
    """Creates and saves a new :class:`Source` and adds a poll task for it.

    Args:
      handler: the current :class:`webapp2.RequestHandler`
      user_url: a string, optional. if provided, supersedes other urls when
        determining the author_url
      **kwargs: passed to :meth:`new()`
    """
    source = cls.new(handler, **kwargs)
    if source is None:
      return None

    if not source.domain_urls:  # defer to the source if it already set this
      auth_entity = kwargs.get('auth_entity')
      if auth_entity and hasattr(auth_entity, 'user_json'):
        source.domain_urls, source.domains = source._urls_and_domains(
          auth_entity, user_url)
    logging.debug('URLs/domains: %s %s', source.domain_urls, source.domains)

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
    logging.info('%s %s', blurb, source.bridgy_url(handler))
    # uncomment to send email notification for each new user
    # if not existing:
    #   util.email_me(subject=blurb, body=source.bridgy_url(handler))

    source.verify()
    if source.verified():
      handler.messages = {blurb}

    # TODO: ugh, *all* of this should be transactional
    source.put()

    if 'webmention' in source.features:
      superfeedr.subscribe(source, handler)

    if 'listen' in source.features:
      util.add_poll_task(source, now=True)
      util.add_poll_task(source, countdown=source.poll_period().total_seconds())

    return source

  def verified(self):
    """Returns True if this source is ready to be used, false otherwise.

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
      force: if True, fully verifies (e.g. re-fetches the blog's HTML and
        performs webmention discovery) even we already think this source is
        verified.
    """
    author_urls = [u for u, d in zip(self.get_author_urls(), self.domains)
                   if not util.in_webmention_blacklist(d)]
    if ((self.verified() and not force) or self.status == 'disabled' or
        not self.features or not author_urls):
      return

    author_url = author_urls[0]
    logging.info('Attempting to discover webmention endpoint on %s', author_url)
    mention = send.WebmentionSend('https://brid.gy/', author_url)
    mention.requests_kwargs = {'timeout': HTTP_TIMEOUT,
                               'headers': util.REQUEST_HEADERS}
    try:
      mention._discoverEndpoint()
    except BaseException:
      logging.info('Error discovering webmention endpoint', exc_info=True)
      mention.error = {'code': 'EXCEPTION'}

    self._fetched_html = getattr(mention, 'html', None)
    error = getattr(mention, 'error', None)
    endpoint = getattr(mention, 'receiver_endpoint', None)
    if error or not endpoint:
      logging.info("No webmention endpoint found: %s %r", error, endpoint)
      self.webmention_endpoint = None
    else:
      logging.info("Discovered webmention endpoint %s", endpoint)
      self.webmention_endpoint = endpoint

    self.put()

  def _urls_and_domains(self, auth_entity, user_url):
    """Returns this user's valid (not webmention-blacklisted) URLs and domains.

    Converts the auth entity's user_json to an ActivityStreams actor and uses
    its 'urls' and 'url' fields. May be overridden by subclasses.

    Args:
      auth_entity: :class:`oauth_dropins.models.BaseAuth`
      user_url: string, optional URL passed in when authorizing

    Returns:
      ([string url, ...], [string domain, ...])
    """
    user = json_loads(auth_entity.user_json)
    actor = (user.get('actor')  # for Instagram; its user_json is IndieAuth
             or self.gr_source.user_to_actor(user))
    logging.debug('Extracting URLs and domains from actor: %s',
                  json_dumps(actor, indent=2))

    candidates = util.trim_nulls(util.uniquify(
        [user_url] + microformats2.object_urls(actor)))

    if len(candidates) > MAX_AUTHOR_URLS:
      logging.info('Too many profile links! Only resolving the first %s: %s',
                   MAX_AUTHOR_URLS, candidates)

    urls = []
    for i, url in enumerate(candidates):
      resolved = self.resolve_profile_url(url, resolve=i < MAX_AUTHOR_URLS)
      if resolved:
        urls.append(resolved)

    final_urls = []
    domains = []
    for url in util.dedupe_urls(urls):  # normalizes domains to lower case
      # skip links on this source's domain itself. only currently needed for
      # Mastodon; the other silo domains are in the webmention blacklist.
      domain = util.domain_from_link(url)
      if domain != self.gr_source.DOMAIN:
        final_urls.append(url)
        domains.append(domain)

    return final_urls, domains

  @staticmethod
  def resolve_profile_url(url, resolve=True):
    """Resolves a profile URL to be added to a source.

    Args:
      url: string
      resolve: boolean, whether to make HTTP requests to follow redirects, etc.

    Returns: string, resolved URL, or None
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
        logging.warning("Couldn't fetch %s, preserving path in %s",
                        root, final, exc_info=True)

    return final

  def canonicalize_url(self, url, activity=None, **kwargs):
    """Canonicalizes a post or object URL.

    Wraps :class:`oauth_dropins.webutil.util.UrlCanonicalizer`.
    """
    return self.URL_CANONICALIZER(url, **kwargs) if self.URL_CANONICALIZER else url

  def infer_profile_url(self, url):
    """Given an arbitrary URL representing a person, try to find their
    profile URL for *this* service.

    Queries Bridgy's registered accounts for users with a particular
    domain in their silo profile.

    Args:
      url: string, a person's URL

    Return:
      a string URL for their profile on this service (or None)
    """
    domain = util.domain_from_link(url)
    if domain == self.gr_source.DOMAIN:
      return url
    user = self.__class__.query(self.__class__.domains == domain).get()
    if user:
      return self.gr_source.user_url(user.key.id())

  def preprocess_for_publish(self, obj):
    """Preprocess an object before trying to publish it.

    By default this tries to massage person tags so that the tag's
    "url" points to the person's profile on this service (as opposed
    to a person's homepage).

    The object is modified in place.

    Args:
      obj: ActivityStreams activity or object dict
    """
    for tag in obj.get('tags', []):
      if tag.get('objectType') == 'person':
        silo_url = None
        for url in microformats2.object_urls(tag):
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
      syndpost: :class:`SyndicatedPost`
    """
    pass

  def is_private(self):
    """Returns True if this source is private aka protected.

    ...ie their posts are not public.
    """
    return False

  def is_activity_public(self, activity):
    """Returns True if the given activity is public, False otherwise.

    Just wraps :meth:`granary.source.Source.is_public`. Subclasses may override.
    """
    return gr_source.Source.is_public(activity)

  def is_beta_user(self):
    """Returns True if this is a "beta" user opted into new features.

    Beta users come from beta_users.txt.
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

  # Turn off instance and memcache caching. See Source for details.
  _use_cache = False
  _use_memcache = False

  source = ndb.KeyProperty()
  status = ndb.StringProperty(choices=STATUSES, default='new')
  leased_until = ndb.DateTimeProperty()
  created = ndb.DateTimeProperty(auto_now_add=True)
  updated = ndb.DateTimeProperty(auto_now=True)

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

  def add_task(self, **kwargs):
    """Adds a propagate task for this entity.

    To be implemented by subclasses.
    """
    raise NotImplementedError()

  @ndb.transactional(xg=True)
  def get_or_save(self):
    existing = self.key.get()
    if existing:
      return existing

    # TODO(ryan): take this out eventually. (and the xg=Trues!) background:
    # https://github.com/snarfed/bridgy/issues/305#issuecomment-94004416
    resp_json = getattr(self, 'response_json', None)
    if resp_json:
      resp = json_loads(resp_json)
      fb_id = resp.get('fb_id')
      if fb_id:
        tag_fb_id = 'tag:facebook.com,2013:' + fb_id
        if tag_fb_id != resp.get('id'):
          resp = Response.get_by_id(tag_fb_id)
          if resp:
            return resp

    if self.unsent or self.error:
      logging.debug('New webmentions to propagate! %s', self.label())
      self.add_task(transactional=True)
    else:
      self.status = 'complete'

    self.put()
    return self

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

    @ndb.transactional
    def finish():
      self.put()
      self.add_task(transactional=True)

    finish()


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
  # to an event.
  old_response_jsons = ndb.TextProperty(repeated=True)
  # JSON dict mapping original post url to activity index in activities_json.
  # only set when there's more than one activity.
  urls_to_activity = ndb.TextProperty()
  # Original post links found by original post discovery
  original_posts = ndb.StringProperty(repeated=True)

  def label(self):
    return ' '.join((self.key.kind(), self.type, self.key.id(),
                     json_loads(self.response_json).get('url', '[no url]')))

  def add_task(self, **kwargs):
    util.add_propagate_task(self, **kwargs)

  @staticmethod
  def get_type(obj):
    type = get_type(obj)
    return type if type in VERB_TYPES else 'comment'

  def get_or_save(self, source, restart=False):
    resp = super(Response, self).get_or_save()

    if (self.type != resp.type or
        source.gr_source.activity_changed(json_loads(resp.response_json),
                                         json_loads(self.response_json),
                                         log=True)):
      logging.info('Response changed! Re-propagating. Original: %s' % resp)
      resp.old_response_jsons = resp.old_response_jsons[:10] + [resp.response_json]
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

    return super(Response, self).restart()


class BlogPost(Webmentions):
  """A blog post to be processed for links to send webmentions to.

  The key name is the URL.
  """
  feed_item = ndb.JsonProperty(compressed=True)  # from Superfeedr

  def label(self):
    url = None
    if self.feed_item:
      url = self.feed_item.get('permalinkUrl')
    return ' '.join((self.key.kind(), self.key.id(), url or '[no url]'))

  def add_task(self, **kwargs):
    util.add_propagate_blogpost_task(self, **kwargs)


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

  # Turn off instance and memcache caching. See Source for details.
  _use_cache = False
  _use_memcache = False

  type = ndb.StringProperty(choices=PUBLISH_TYPES)
  status = ndb.StringProperty(choices=STATUSES, default='new')
  source = ndb.KeyProperty()
  html = ndb.TextProperty()  # raw HTML fetched from source
  published = ndb.JsonProperty(compressed=True)
  created = ndb.DateTimeProperty(auto_now_add=True)
  updated = ndb.DateTimeProperty(auto_now=True)

  def type_label(self):
    """Returns silo-specific string type, e.g. 'favorite' instead of 'like'."""
    for cls in sources.values():  # global
      if cls.__name__ == self.source.kind():
        return cls.TYPE_LABELS.get(self.type, self.type)

    return self.type


class BlogWebmention(Publish, StringIdModel):
  """Datastore entity for webmentions for hosted blog providers.

  Key id is the source URL and target URL concated with a space, ie 'SOURCE
  TARGET'. The source URL is *always* the URL given in the webmention HTTP
  request. If the source page has a u-url, that's stored in the u_url property.
  The target URL is always the final URL, after any redirects.

  Reuses :class:`Publish`'s fields, but otherwise unrelated.
  """
  # If the source page has a u-url, it's stored here and overrides the source
  # URL in the key id.
  u_url = ndb.StringProperty()

  # Any initial target URLs that redirected to the final target URL, in redirect
  # order.
  redirected_target_urls = ndb.StringProperty(repeated=True)

  def source_url(self):
    return self.u_url or self.key.id().split()[0].decode('utf-8')

  def target_url(self):
    return self.key.id().split()[1]


class SyndicatedPost(ndb.Model):
  """Represents a syndicated post and its discovered original (or not
  if we found no original post).  We discover the relationship by
  following rel=syndication links on the author's h-feed.

  See :mod:`original_post_discovery`.

  When a :class:`SyndicatedPost` entity is about to be stored,
  :meth:`source.Source.on_new_syndicated_post()` is called before it's stored.
  """

  # Turn off instance and memcache caching. See Response for details.
  _use_cache = False
  _use_memcache = False

  syndication = ndb.StringProperty()
  original = ndb.StringProperty()
  created = ndb.DateTimeProperty(auto_now_add=True)
  updated = ndb.DateTimeProperty(auto_now=True)

  @classmethod
  @ndb.transactional(xg=True)
  def insert_original_blank(cls, source, original):
    """Insert a new original -> None relationship. Does a check-and-set to
    make sure no previous relationship exists for this original. If
    there is, nothing will be added.

    Args:
      source: :class:`Source` subclass
      original: string
    """
    if cls.query(cls.original == original, ancestor=source.key).get():
      return
    cls(parent=source.key, original=original, syndication=None).put()

  @classmethod
  @ndb.transactional(xg=True)
  def insert_syndication_blank(cls, source, syndication):
    """Insert a new syndication -> None relationship. Does a check-and-set
    to make sure no previous relationship exists for this
    syndication. If there is, nothing will be added.

    Args:
      source: :class:`Source` subclass
      original: string
    """

    if cls.query(cls.syndication == syndication, ancestor=source.key).get():
      return
    cls(parent=source.key, original=None, syndication=syndication).put()

  @classmethod
  @ndb.transactional(xg=True)
  def insert(cls, source, syndication, original):
    """Insert a new (non-blank) syndication -> original relationship.

    This method does a check-and-set within transaction to avoid
    including duplicate relationships.

    If blank entries exists for the syndication or original URL
    (i.e. syndication -> None or original -> None), they will first be
    removed. If non-blank relationships exist, they will be retained.

    Args:
      source: :class:`Source` subclass
      syndication: string (not None)
      original: string (not None)

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
    ndb.delete_multi(
      cls.query(ndb.OR(
        ndb.AND(cls.syndication == syndication, cls.original == None),
        ndb.AND(cls.original == original, cls.syndication == None)),
                ancestor=source.key).fetch(keys_only=True))

    r = cls(parent=source.key, original=original, syndication=syndication)
    r.put()
    return r

  def _pre_put_hook(self):
    self.key.parent().get().on_new_syndicated_post(self)
