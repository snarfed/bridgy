"""Datastore model classes.
"""

import datetime
import itertools
import json
import logging
import re
import urllib
import urlparse

import appengine_config
from appengine_config import HTTP_TIMEOUT, DEBUG

from activitystreams import source as as_source
from activitystreams.oauth_dropins.webutil.models import StringIdModel
import superfeedr
import util
from webmentiontools import send

from google.appengine.api import taskqueue
from google.appengine.api import users
from google.appengine.ext import ndb


VERB_TYPES = ('comment', 'like', 'repost', 'rsvp')
TYPES = VERB_TYPES + ('post', 'preview')

def get_type(obj):
  """Returns the Response or Publish type for an ActivityStreams object."""
  type = obj.get('objectType')
  verb = obj.get('verb')
  if type == 'activity' and verb == 'share':
    return 'repost'
  elif verb in VERB_TYPES:
    return verb
  elif verb in as_source.RSVP_TO_EVENT:
    return 'rsvp'
  elif type == 'comment':
    return 'comment'
  else:
    return 'post'


class DisableSource(Exception):
  """Raised when a user has deauthorized our app inside a given platform.
  """


class Source(StringIdModel):
  """A silo account, e.g. a Facebook or Google+ account.

  Each concrete silo class should subclass this class.
  """
  STATUSES = ('enabled', 'disabled', 'error')
  FEATURES = ('listen', 'publish', 'webmention')

  # short name for this site type. used in URLs, ec.
  SHORT_NAME = None
  # the corresponding activitystreams-unofficial class
  AS_CLASS = None

  # how often to poll for responses
  FAST_POLL = datetime.timedelta(minutes=10)
  # poll sources less often (this much) if they've never sent a webmention
  SLOW_POLL = datetime.timedelta(days=1)
  # how long to wait after signup for a successful webmention before dropping to
  # the lower frequency poll
  FAST_POLL_GRACE_PERIOD = datetime.timedelta(days=7)
  # refetch author url to look for updated syndication links
  REFETCH_PERIOD = datetime.timedelta(hours=2)

  # Maps Publish.type (e.g. 'like') to source-specific human readable type label
  # (e.g. 'favorite'). Subclasses should override this.
  TYPE_LABELS = {}

  created = ndb.DateTimeProperty(auto_now_add=True, required=True)
  url = ndb.StringProperty()
  status = ndb.StringProperty(choices=STATUSES, default='enabled')
  name = ndb.StringProperty()  # full human-readable name
  picture = ndb.StringProperty()
  domains = ndb.StringProperty(repeated=True)
  domain_urls = ndb.StringProperty(repeated=True)
  features = ndb.StringProperty(repeated=True, choices=FEATURES)
  superfeedr_secret = ndb.StringProperty()
  webmention_endpoint = ndb.StringProperty()

  last_polled = ndb.DateTimeProperty(default=util.EPOCH)
  last_poll_attempt = ndb.DateTimeProperty(default=util.EPOCH)
  last_webmention_sent = ndb.DateTimeProperty()  # currently only used for listen

  # the last time we re-fetched the author's url looking for updated
  # syndication links
  last_hfeed_fetch = ndb.DateTimeProperty(default=util.EPOCH)

  # the last time we've seen a rel=syndication link for this Source.
  # we won't spend the time to re-fetch and look for updates if there's
  # never been one
  last_syndication_url = ndb.DateTimeProperty()

  # points to an oauth-dropins auth entity. The model class should be a subclass
  # of oauth_dropins.BaseAuth.
  # the token should be generated with the offline_access scope so that it
  # doesn't expire. details: http://developers.facebook.com/docs/authentication/
  auth_entity = ndb.KeyProperty()

  last_activity_id = ndb.StringProperty()
  last_activities_etag = ndb.StringProperty()

  # as_source is *not* set to None by default here, since it needs to be unset
  # for __getattr__ to run when it's accessed.

  def new(self, **kwargs):
    """Factory method. Creates and returns a new instance for the current user.

    To be implemented by subclasses.
    """
    raise NotImplementedError()

  def __getattr__(self, name):
    """Lazily load the auth entity and instantiate self.as_source.

    Once self.as_source is set, this method will *not* be called; the as_source
    attribute will be returned normally.
    """
    if name == 'as_source' and self.auth_entity:
      token = self.auth_entity.get().access_token()
      if not isinstance(token, tuple):
        token = (token,)
      self.as_source = self.AS_CLASS(*token)
      return self.as_source

    return getattr(super(Source, self), name)

  def bridgy_path(self):
    """Returns the Bridgy page URL path for this source."""
    return '/%s/%s' % (self.SHORT_NAME,self.key.string_id())

  def bridgy_url(self, handler):
    """Returns the Bridgy page URL for this source."""
    return handler.request.host_url + self.bridgy_path()

  def silo_url(self, handler):
    """Returns the silo account URL.g. https://twitter.com/foo."""
    raise NotImplementedError()

  def label(self):
    """Human-readable label for this site."""
    return '%s (%s)' % (self.name, self.AS_CLASS.NAME)

  def poll_period(self):
    """Returns the poll frequency for this source.

    Defaults to ~15m, depending on silo. If we've never sent a webmention for
    this source, after a grace period we drop them down to ~1d.
    """
    now = datetime.datetime.now()
    return (self.SLOW_POLL
            if (now > self.created + self.FAST_POLL_GRACE_PERIOD and
                not self.last_webmention_sent)
            else self.FAST_POLL)

  def refetch_period(self):
    """Returns the refetch frequency for this source.

    Note that refetch will only kick in if certain conditions are
    met.
    """
    return self.REFETCH_PERIOD

  @classmethod
  def bridgy_webmention_endpoint(cls):
    """Returns the Bridgy webmention endpoint for this source type."""
    return 'https://www.brid.gy/webmention/' + cls.SHORT_NAME

  def get_author_url(self):
    """Determine the author url for a particular source.
    In debug mode, replace test domains with localhost

    Return:
      a string, the author's url or None
    """
    return (util.replace_test_domains_with_localhost(self.domain_urls[0])
            if self.domain_urls else None)

  def get_activities_response(self, **kwargs):
    """Returns recent posts and embedded comments for this source.

    Passes through to activitystreams-unofficial by default. May be overridden
    by subclasses.
    """
    return self.as_source.get_activities_response(group_id=as_source.SELF, **kwargs)

  def get_activities(self, *args, **kwargs):
    return self.get_activities_response(*args, **kwargs)['items']

  def get_post(self, id):
    """Returns a post from this source.

    Args:
      id: string, site-specific post id

    Returns: dict, decoded ActivityStreams activity, or None
    """
    activities = self.get_activities(activity_id=id, user_id=self.key.string_id())
    return activities[0] if activities else None

  def get_comment(self, comment_id, activity_id=None, activity_author_id=None):
    """Returns a comment from this source.

    Passes through to activitystreams-unofficial by default. May be overridden
    by subclasses.

    Args:
      comment_id: string, site-specific comment id
      activity_id: string, site-specific activity id
      activity_author_id: string, site-specific activity author id, optional

    Returns: dict, decoded ActivityStreams comment object, or None
    """
    return self.as_source.get_comment(comment_id, activity_id=activity_id,
                                      activity_author_id=activity_author_id)

  def get_like(self, activity_user_id, activity_id, like_user_id):
    """Returns an ActivityStreams 'like' activity object.

    Passes through to activitystreams-unofficial by default. May be overridden
    by subclasses.

    Args:
      activity_user_id: string id of the user who posted the original activity
      activity_id: string activity id
      like_user_id: string id of the user who liked the activity
    """
    return self.as_source.get_like(activity_user_id, activity_id, like_user_id)

  def get_share(self, activity_user_id, activity_id, share_id):
    """Returns an ActivityStreams 'share' activity object.

    Passes through to activitystreams-unofficial by default. May be overridden
    by subclasses.

    Args:
      activity_user_id: string id of the user who posted the original activity
      activity_id: string activity id
      share_id: string id of the share object or the user who shared it
    """
    return self.as_source.get_share(activity_user_id, activity_id, share_id)

  def get_rsvp(self, activity_user_id, event_id, user_id):
    """Returns an ActivityStreams 'rsvp-*' activity object.

    Passes through to activitystreams-unofficial by default. May be overridden
    by subclasses.

    Args:
      activity_user_id: string id of the user who posted the original activity
      event_id: string event id
      user_id: string id of the user object or the user who RSVPed
    """
    return self.as_source.get_rsvp(activity_user_id, event_id, user_id)

  def create_comment(self, post_url, author_name, author_url, content):
    """Creates a new comment in the source silo.

    Must be implemented by subclasses.

    Args:
      post_url: string
      author_name: string
      author_url: string
      content: string

    Returns: response dict with at least 'id' field
    """
    raise NotImplementedError()

  def feed_url(self):
    """Returns the RSS or Atom (or similar) feed URL for this source.

    Must be implemented by subclasses. Currently only implemented by Blogger,
    Tumlbr, and WordPress.

    Returns: string URL
    """
    raise NotImplementedError()

  def edit_template_url(self):
    """Returns the URL for editing this blog's template HTML.

    Must be implemented by subclasses. Currently only implemented by Blogger,
    Tumlbr, and WordPress.

    Returns: string URL
    """
    raise NotImplementedError()

  @classmethod
  def create_new(cls, handler, **kwargs):
    """Creates and saves a new Source and adds a poll task for it.

    Args:
      handler: the current RequestHandler
      **kwargs: passed to new()
    """
    source = cls.new(handler, **kwargs)
    if source is None:
      return None

    feature = source.features[0] if source.features else 'listen'

    if not source.domain_urls:
      # extract domain from the URL set on the user's profile, if any
      auth_entity = kwargs.get('auth_entity')
      if auth_entity and hasattr(auth_entity, 'user_json'):
        url, domain, ok = source._url_and_domain(auth_entity)
        if feature == 'publish' and not ok:
          if not url:
            handler.messages = {'Your %s profile is missing the website field. '
                                'Please add it and try again!' % cls.AS_CLASS.NAME}
          elif not domain:
            handler.messages = {'Could not parse the web site in your %s profile: '
                                '%s\n Please update it and try again!' %
                                (cls.AS_CLASS.NAME, url)}
          else:
            handler.messages = {"Could not connect to the web site in your %s profile: "
                                "%s\n Please update it and try again!" %
                                (cls.AS_CLASS.NAME, url)}
          return None

        if ok:
          if not source.domain_urls:
            source.domain_urls = [url]
          if not source.domains:
            source.domains = [domain]

    # check if this source already exists
    existing = source.key.get()
    if existing:
      # merge some fields
      source.features = set(source.features + existing.features)
      source.populate(**existing.to_dict(include=(
            'created', 'last_hfeed_fetch', 'last_poll_attempt', 'last_polled',
            'last_syndication_url', 'last_webmention_sent', 'superfeedr_secret')))
      verb = 'Updated'
    else:
      verb = 'Added'

    link = ('http://indiewebify.me/send-webmentions/?url=' + source.domain_urls[0]
            if source.domain_urls else 'http://indiewebify.me/#send-webmentions')
    blurb = '%s %s. %s' % (verb, source.label(), {
      'listen': "Refresh to see what we've found!",
      'publish': 'Try previewing a post from your web site!',
      'webmention': '<a href="%s">Try a webmention!</a>' % link,
      }.get(feature, ''))
    logging.info('%s %s', blurb, source.bridgy_url(handler))
    if not existing:
      util.email_me(subject=blurb, body=source.bridgy_url(handler))

    source.verify()
    if source.verified():
      handler.messages = {blurb}

    if 'webmention' in source.features:
      superfeedr.subscribe(source, handler)

    # TODO: ugh, *all* of this should be transactional
    source.put()

    if 'listen' in source.features:
      util.add_poll_task(source)

    return source

  def verified(self):
    """Returns True if this source is ready to be used, false otherwise.

    See verify() for details. May be overridden by subclasses, e.g. Tumblr.
    """
    return not ('webmention' in self.features and not self.webmention_endpoint)

  def verify(self, force=False):
    """Checks that this source is ready to be used.

    For blog sources, this fetches their front page HTML and checks that they're
    advertising the Bridgy webmention endpoint. For other kinds of sources, this
    currently does nothing.

    May be overridden by subclasses, e.g. Tumblr.

    Args:
      force: if True, fully verifies (e.g. re-fetches the blog's HTML and looks
        perform webmention discovery) even we already think this source is
        verified.
    """
    if self.verified() and not force:
      return

    logging.info('Attempting to discover webmention endpoint on %s',
                 self.domain_urls[0])
    mention = send.WebmentionSend('https://www.brid.gy/', self.domain_urls[0])
    mention.requests_kwargs = {'timeout': HTTP_TIMEOUT}
    try:
      mention._discoverEndpoint()
    except BaseException:
      logging.warning('', exc_info=True)
      mention.error = {'code': 'EXCEPTION'}

    self._fetched_html = getattr(mention, 'html', None)
    error = getattr(mention, 'error', None)
    endpoint = getattr(mention, 'receiver_endpoint', None)
    if error or not endpoint:
      logging.error("No webmention endpoint found: %s %r", error, endpoint)
      self.webmention_endpoint = None
    else:
      logging.info("Discovered webmention endpoint %s", endpoint)
      self.webmention_endpoint = endpoint

    self.put()

  def _url_and_domain(self, auth_entity):
    """Returns this source's URL and domain.

    Uses the auth entity user_json 'url' field by default. May be overridden
    by subclasses.

    Args:
      auth_entity: oauth_dropins.models.BaseAuth

    Returns: (string url, string domain, boolean ok) tuple
    """
    actor = self.as_source.user_to_actor(json.loads(auth_entity.user_json))
    # TODO: G+ has a multiply-valued 'urls' field. ignoring for now because
    # we're not implementing publish for G+
    domain = None
    ok = False

    url = actor.get('url')
    if url:
      url = url.split()[0]
      url, domain, ok = util.get_webmention_target(url)
      if ok:
        domain = domain.lower()

    return url, domain, ok

  def canonicalize_syndication_url(self, syndication_url, scheme='https'):
    """Perform source-specific transforms to the syndication URL for cases
    where multiple silo URLs can point to the same content.  By
    standardizing on one format, original_post_discovery stands the
    best chance of finding the relationship between the original and
    its syndicated copies.

    Args:
      syndication_url: a string, the url of the syndicated content
      scheme: a string, the canonical scheme for this source (https by default)

    Return:
      a string, the canonical form of the syndication url
    """
    return re.sub('^https?://(www\.)?', scheme + '://', syndication_url)

  def preprocess_superfeedr_item(self, item):
    """Process a Superfeeder item field before extracting links from it.

    The item is modified in place. Default is noop. Indivudal sources can
    override this with source-specific logic.

    I used to use this to remove WordPress.com category and tag links from
    content, but I ended up just filtering out all self-links instead. Details
    in https://github.com/snarfed/bridgy/issues/207.

    TODO: remove this if it ends up unused

    Args:
      item: dict, a JSON Superfeedr feed item. Details:
            http://documentation.superfeedr.com/schema.html#json
    """
    pass


class Webmentions(StringIdModel):
  """A bundle of links to send webmentions for.

  Use the Response and BlogPost concrete subclasses below.
  """
  STATUSES = ('new', 'processing', 'complete', 'error')

  # Turn off NDB instance and memcache caching. Main reason is to improve memcache
  # hit rate since app engine only gives me 1MB right now. :/ Background:
  # https://github.com/snarfed/bridgy/issues/68
  #
  # If you re-enable caching, MAKE SURE YOU re-enable the global ban on instance
  # caching in appengine_config.py.
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

  def add_task(self):
    """Adds a propagate task for this entity.

    To be implemented by subclasses.
    """
    raise NotImplementedError()

  @ndb.transactional
  def get_or_save(self):
    existing = self.key.get()
    if existing:
      # logging.debug('Deferring to existing response %s.', existing.key.string_id())
      # this might be a nice sanity check, but we'd need to hard code certain
      # properties (e.g. content) so others (e.g. status) aren't checked.
      # for prop in self.properties().values():
      #   new = prop.get_value_for_datastore(self)
      #   existing = prop.get_value_for_datastore(existing)
      #   assert new == existing, '%s: new %s, existing %s' % (prop, new, existing)
      return existing

    logging.debug('New webmentions to propagate! %s', self.label())
    self.put()
    self.add_task()
    return self


class Response(Webmentions):
  """A comment, like, or repost to be propagated.

  The key name is the comment object id as a tag URI.
  """
  # ActivityStreams JSON activity and comment, like, or repost
  type = ndb.StringProperty(choices=VERB_TYPES, default='comment')
  activity_json = ndb.TextProperty()
  response_json = ndb.TextProperty()

  def label(self):
    return ' '.join((self.key.kind(), self.type, self.key.id(),
                     json.loads(self.response_json).get('url', '[no url]')))

  def add_task(self):
    util.add_propagate_task(self)

  @staticmethod
  def get_type(obj):
    type = get_type(obj)
    return type if type in VERB_TYPES else 'comment'


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

  def add_task(self):
    util.add_propagate_blogpost_task(self)


class PublishedPage(StringIdModel):
  """Minimal root entity for Publish children entities with the same source URL.

  Key id is the string source URL.
  """
  pass


class Publish(ndb.Model):
  """A comment, like, repost, or RSVP published into a silo.

  Child of a PublishedPage entity.
  """
  STATUSES = ('new', 'complete', 'failed')

  # Turn off instance and memcache caching. See Response for details.
  _use_cache = False
  _use_memcache = False

  type = ndb.StringProperty(choices=TYPES)
  type_label = ndb.StringProperty()  # source-specific type, e.g. 'favorite'
  status = ndb.StringProperty(choices=STATUSES, default='new')
  source = ndb.KeyProperty()
  html = ndb.TextProperty()  # raw HTML fetched from source
  published = ndb.JsonProperty(compressed=True)
  created = ndb.DateTimeProperty(auto_now_add=True)
  updated = ndb.DateTimeProperty(auto_now=True)


class BlogWebmention(Publish, StringIdModel):
  """Datastore entity for webmentions for hosted blog providers.

  Key id is the source URL and target URL concated with a space, ie 'SOURCE
  TARGET'. The source URL is *always* the URL given in the webmention HTTP
  request. If the source page has a u-url, that's stored in the u_url property.

  Reuses Publish's fields, but otherwise unrelated.
  """
  # If the source page has a u-url, it's stored here and overrides the source
  # URL in the key id.
  u_url = ndb.StringProperty()

  def source_url(self):
    return self.u_url or self.key.id().split()[0]

  def target_url(self):
    return self.key.id().split()[1]


class SyndicatedPost(ndb.Model):
  """Represents a syndicated post and its discovered original (or not
  if we found no original post).  We discover the relationship by
  following rel=syndication links on the author's h-feed.

  See original_post_discovery.
  """

  # Turn off instance and memcache caching. See Response for details.
  _use_cache = False
  _use_memcache = False

  syndication = ndb.StringProperty()
  original = ndb.StringProperty()
  created = ndb.DateTimeProperty(auto_now_add=True)
  updated = ndb.DateTimeProperty(auto_now=True)

  @classmethod
  def query_by_original(cls, source, url):
    return cls.query(cls.original == url,
                     ancestor=source.key).get()

  @classmethod
  def query_by_originals(cls, source, urls):
    # 30 item limit for IN queries, chain together multiple queries
    urls = list(urls)
    return itertools.chain.from_iterable(
        cls.query(cls.original.IN(urls[i:i + 30]), ancestor=source.key)
        for i in xrange(0, len(urls), 30))

  @classmethod
  def query_by_syndication(cls, source, url):
    return cls.query(cls.syndication == url,
                     ancestor=source.key).get()

  @classmethod
  @ndb.transactional
  def get_or_insert_by_syndication_url(cls, source, syndication,
                                       original):
    """Insert a relationship from syndication-url -> original, replacing
    blank placeholder relationships if they exist.

    This does a check-and-set inside a transaction to avoid putting
    duplicates in the database because we assume each syndication URL
    can only have one original. If there is already a non-blank
    SyndicatedPost for this syndication URL, this function will return
    without saving anything.

    If there is a pre-existing non-blank SyndicationPost for this
    original, this function will add another relationship for the same
    original.

    If there are pre-existing syndication->None or original->None
    relationships, this function will remove them before adding a
    new non-blank relationship.

    Args:
      source: models.Source subclass
      syndication: string
      original: string
    """
    relationship = cls.query_by_syndication(source, syndication)

    # do not overwrite a preexisting relationship
    if relationship and relationship.original:
      return relationship

    # if this is a non-blank relationship, remove pre-existing blanks
    if original and syndication:
      # remove syndication->None relationships
      if relationship and not relationship.original:
        relationship.key.delete()

      # remove original->None relationships too
      rel_by_original = cls.query_by_original(source, original)
      if rel_by_original and not rel_by_original.syndication:
        rel_by_original.key.delete()

    relationship = cls(parent=source.key, original=original,
                       syndication=syndication)
    relationship.put()
    return relationship
