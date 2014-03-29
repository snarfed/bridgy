"""Datastore model classes.
"""

import datetime
import itertools
import json
import logging
import urllib
import urlparse

import appengine_config

from activitystreams import source as as_source
from activitystreams.oauth_dropins.webutil.models import StringIdModel
import util

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
  FEATURES = ('listen', 'publish')

  # short name for this site type. used in URLs, ec.
  SHORT_NAME = None
  # the corresponding activitystreams-unofficial class
  AS_CLASS = None
  POLL_FREQUENCY = datetime.timedelta(minutes=10)
  # Maps Publish.type (e.g. 'like') to source-specific human readable type label
  # (e.g. 'favorite'). Subclasses should override this.
  TYPE_LABELS = {}

  created = ndb.DateTimeProperty(auto_now_add=True, required=True)
  url = ndb.StringProperty()
  status = ndb.StringProperty(choices=STATUSES, default='enabled')
  name = ndb.StringProperty()  # full human-readable name
  picture = ndb.StringProperty()
  domain = ndb.StringProperty()
  domain_url = ndb.StringProperty()
  features = ndb.StringProperty(repeated=True, choices=FEATURES)

  last_polled = ndb.DateTimeProperty(default=util.EPOCH)
  last_poll_attempt = ndb.DateTimeProperty(default=util.EPOCH)

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

  def label(self):
    """Human-readable label for this site."""
    return '%s (%s)' % (self.name, self.AS_CLASS.NAME)

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

  @classmethod
  def create_new(cls, handler, **kwargs):
    """Creates and saves a new Source and adds a poll task for it.

    Args:
      handler: the current RequestHandler
      **kwargs: passed to new()
    """
    source = cls.new(handler, **kwargs)
    feature = source.features[0] if source.features else 'listen'

    # extract domain from the URL set on the user's profile, if any
    auth_entity = kwargs.get('auth_entity')
    if auth_entity and auth_entity.user_json:
      actor = source.as_source.user_to_actor(json.loads(auth_entity.user_json))
      # TODO: G+ has a multiply-valued 'urls' field. ignoring for now because
      # we're not implementing publish for G+
      url = actor.get('url')
      ok = False
      if url:
        url = url.split()[0]
        resolved, domain, ok = util.get_webmention_target(url)
        if ok:
          source.domain = domain
          source.domain_url = resolved

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
        handler.messages_error = 'error'
        return None

    # check if this source already exists
    existing = source.key.get()
    if existing:
      # merge some fields
      source.features = set(source.features + existing.features)
      verb = 'Updated'
    else:
      verb = 'Added'

    blurb = '%s %s' % (verb, source.label())
    handler.messages = {
          ("%s. Refresh to see what we've found!" if feature == 'listen' else
           '%s. Try previewing a post from your web site!') % blurb}
    logging.info('%s %s', blurb, source.bridgy_url(handler))
    util.email_me(subject=blurb, body=source.bridgy_url(handler))

    # TODO: ugh, *all* of this should be transactional
    source.put()

    if 'listen' in source.features:
      util.add_poll_task(source)

    return source


class Response(StringIdModel):
  """A comment, like, or repost to be propagated.

  The key name is the comment object id as a tag URI.
  """
  STATUSES = ('new', 'processing', 'complete', 'error')

  # ActivityStreams JSON activity and comment, like, or repost
  type = ndb.StringProperty(choices=VERB_TYPES, default='comment')
  activity_json = ndb.TextProperty()
  response_json = ndb.TextProperty()
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

    obj = json.loads(self.response_json)
    self.type = Response.get_type(obj)
    logging.debug('New response to propagate! %s %s %s', self.type,
                  self.key.id(),  # returns either string name or integer id
                  obj.get('url', '[no url]'))

    self.put()
    util.add_propagate_task(self)
    return self

  @staticmethod
  def get_type(obj):
    type = get_type(obj)
    return type if type in VERB_TYPES else 'comment'


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

  type = ndb.StringProperty(choices=TYPES)
  type_label = ndb.StringProperty()  # source-specific type, e.g. 'favorite'
  status = ndb.StringProperty(choices=STATUSES, default='new')
  source = ndb.KeyProperty()
  html = ndb.TextProperty()  # raw HTML fetched from source
  published = ndb.JsonProperty(compressed=True)
  created = ndb.DateTimeProperty(auto_now_add=True)
  updated = ndb.DateTimeProperty(auto_now=True)
