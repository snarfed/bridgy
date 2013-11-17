"""Datastore model classes.
"""

import datetime
import itertools
import json
import logging
import urlparse

import appengine_config
import util
from webutil.models import KeyNameModel

from google.appengine.api import taskqueue
from google.appengine.api import users
from google.appengine.ext import db


class User(db.Model):
  """A registered user.

  The key name is either App Engine user_id or OpenID federated_identity.
  """

  @classmethod
  def get_current_user(cls):
    key_name = cls._current_user_key_name()
    if key_name:
      return cls.get_by_key_name(key_name)

  @classmethod
  @db.transactional
  def get_or_insert_current_user(cls, handler):
    """Returns the logged in user's User instance, creating it if necessary.

    Implemented manually instead of via Model.get_or_insert() because we want to
    know if we created the User object so we can add a message to the handler.

    Args:
      handler: the current RequestHandler
    """
    key_name = cls._current_user_key_name()
    if key_name:
      user = cls.get_by_key_name(key_name)
      if not user:
        user = cls(key_name=key_name)
        user.save()
        handler.messages.append('Registered new user.')

      return user

  @staticmethod
  def _current_user_key_name():
    """Returns a unique key name for the current user.

    Returns: the user's OpenId identifier or App Engine user id or None if
      they're not logged in
    """
    user = users.get_current_user()
    if user:
      return user.federated_identity() or user.user_id()


class Site(KeyNameModel):
  """A web site for a single entity, e.g. Facebook profile or WordPress blog.
  """

  # human-readable name for this site type. subclasses should override.
  TYPE_NAME = None
  STATUSES = ('enabled', 'disabled')

  created = db.DateTimeProperty(auto_now_add=True, required=True)
  url = db.LinkProperty()
  status = db.StringProperty(choices=STATUSES, default='enabled')

  def display_name(self):
    """Returns a human-readable name for this site, e.g. 'My Thoughts'.

    Defaults to the url. May be overridden by subclasses.
    """
    # TODO: get this from the site itself, e.g. <title> in <head>
    return util.domain_from_link(self.url)

  def type_display_name(self):
    """Returns a human-readable name for this type of site, e.g. 'Facebook'.

    May be overridden by subclasses.
    """
    return self.TYPE_NAME

  def label(self):
    """Human-readable label for this site."""
    return '%s: %s' % (self.type_display_name(), self.display_name())

  @classmethod
  def create_new(cls, handler, **kwargs):
    """Creates and saves a new Site.

    Args:
      handler: the current RequestHandler
      **kwargs: passed to new()
    """
    new = cls.new(handler, **kwargs)
    existing = db.get(new.key())
    if existing:
      logging.warning('Overwriting %s %s! Old version:\n%s',
                      existing.label(), new.key(), new.to_xml())
      handler.messages.append('Updated existing %s' % existing.label())
    else:
      handler.messages.append('Added %s' % new.label())

    # TODO: ugh, *all* of this should be transactional
    new.save()
    return new


class Source(Site):
  """A silo account, e.g. a Facebook or Google+ account.

  Each concrete silo class should subclass this class.
  """

  last_polled = db.DateTimeProperty(default=util.EPOCH)

  # full human-readable name
  name = db.StringProperty()
  picture = db.LinkProperty()

  # points to an oauth-dropins auth entity. The model class should be a subclass
  # of oauth_dropins.BaseAuth.
  # the token should be generated with the offline_access scope so that it
  # doesn't expire. details: http://developers.facebook.com/docs/authentication/
  auth_entity = db.ReferenceProperty()

  # An activitystreams-unofficial source instance. Initialized in the ctor if
  # self.auth_entity is set.
  as_source = None

  def new(self, **kwargs):
    """Factory method. Creates and returns a new instance for the current user.

    To be implemented by subclasses.
    """
    raise NotImplementedError()

  def get_post(self, id):
    """Returns a post from this source.

    Args:
      id: string, site-specific post id

    Returns: dict, decoded ActivityStreams activity, or None
    """
    count, activities = self.as_source.get_activities(activity_id=id)
    return activities[0]['object'] if activities else None

  def get_comment(self, id):
    """Returns a comment from this source.

    Args:
      id: string, site-specific comment id

    Returns: dict, decoded ActivityStreams comment object, or None
    """
    return self.as_source.get_comment(id)

  def get_comments(self):
    """Returns a list of Comment instances for recent posts from this source.

    To be implemented by subclasses.
    """
    raise NotImplementedError()

  @classmethod
  def create_new(cls, handler, **kwargs):
    """Creates and saves a new Source and adds a poll task for it.

    Args:
      handler: the current RequestHandler
      **kwargs: passed to new()
    """
    new = super(Source, cls).create_new(handler, **kwargs)
    util.add_poll_task(new)
    return new


class Comment(KeyNameModel):
  """A comment to be propagated.
  """
  STATUSES = ('new', 'processing', 'complete')

  # microformats2 json. sources may store extra source-specific properties.
  mf2_json = db.TextProperty()
  source = db.ReferenceProperty()
  status = db.StringProperty(choices=STATUSES, default='new')
  leased_until = db.DateTimeProperty()

  @db.transactional
  def get_or_save(self):
    existing = db.get(self.key())
    if existing:
      # logging.debug('Deferring to existing comment %s.', existing.key().name())
      # this might be a nice sanity check, but we'd need to hard code certain
      # properties (e.g. content) so others (e.g. status) aren't checked.
      # for prop in self.properties().values():
      #   new = prop.get_value_for_datastore(self)
      #   existing = prop.get_value_for_datastore(existing)
      #   assert new == existing, '%s: new %s, existing %s' % (prop, new, existing)
      return existing

    props = json.loads(self.mf2_json)['properties']
    logging.debug('New comment to propagate! %s %r\n%s on %s',
                  self.kind(), self.key().id_or_name(),
                  props.get('url'), props.get('in-reply-to'))
    taskqueue.add(queue_name='propagate', params={'comment_key': str(self.key())})
    self.save()
    return self


class DisableSource(Exception):
  """Raised when a user has deauthorized our app inside a given platform.
  """
