"""Datastore model classes.
"""

import datetime
import itertools
import logging
import urlparse

import appengine_config
import util

from google.appengine.api import taskqueue
from google.appengine.api import users
from google.appengine.ext import db
from google.appengine.ext import webapp
from google.appengine.ext.webapp.util import run_wsgi_app


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
      handler: the current webapp.RequestHandler
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


class Site(util.KeyNameModel):
  """A web site for a single entity, e.g. Facebook profile or WordPress blog.

  Not intended to be used directly. Inherit from one or both of the Destination
  and Source subclasses.
  """

  # human-readable name for this destination type. subclasses should override.
  TYPE_NAME = None
  STATUSES = ('enabled', 'disabled')

  created = db.DateTimeProperty(auto_now_add=True, required=True)
  url = db.LinkProperty()
  owner = db.ReferenceProperty(User)
  status = db.StringProperty(choices=STATUSES, default='enabled')

  def display_name(self):
    """Returns a human-readable name for this site, e.g. 'My Thoughts'.

    Defaults to the url. May be overridden by subclasses.
    """
    # TODO: get this from the site itself, e.g. <title> in <head>
    return util.reduce_url(self.url)

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
      handler: the current webapp.RequestHandler
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
  """A web site to read comments from, e.g. a Facebook profile.

  Each concrete source class should subclass this class.
  """

  last_polled = db.DateTimeProperty(default=util.EPOCH)

  def new(self, **kwargs):
    """Factory method. Creates and returns a new instance for the current user.

    To be implemented by subclasses.
    """
    raise NotImplementedError()

  def get_posts(self):
    """Returns a list of the most recent posts from this source.

    To be implemented by subclasses. The returned post objects will be passed
    back in get_comments().

    Returns: list of (post, url), where post is any object and url is the string
      url for the post
    """
    raise NotImplementedError()

  def get_comments(self, posts_and_dests):
    """Returns a list of Comment instances for the given posts.

    To be implemented by subclasses. Only called after get_posts().

    Args:
      posts_and_dests: list of (post object, Destination) tuples. The post
        objects are a subset of the ones returned by get_posts().
    """
    raise NotImplementedError()

  @classmethod
  def create_new(cls, handler, **kwargs):
    """Creates and saves a new Source and adds a poll task for it.

    Args:
      handler: the current webapp.RequestHandler
      **kwargs: passed to new()
    """
    new = super(Source, cls).create_new(handler, **kwargs)
    util.add_poll_task(new)
    return new


class Destination(Site):
  """A web site to propagate comments to, e.g. a WordPress blog.

  Each concrete destination class should subclass this class.
  """

  last_updated = db.DateTimeProperty()

  def add_comment(self, comment):
    """Posts the given comment to this site.

    To be implemented by subclasses.

    Args:
      comment: Comment
    """
    raise NotImplementedError()


class Comment(util.KeyNameModel):
  """A comment to be propagated.
  """
  STATUSES = ('new', 'processing', 'complete')

  source = db.ReferenceProperty(reference_class=Source, required=True)
  dest = db.ReferenceProperty(reference_class=Destination, required=True)
  source_post_url = db.LinkProperty()
  source_comment_url = db.LinkProperty()
  dest_post_url = db.LinkProperty()
  dest_comment_url = db.LinkProperty()
  created = db.DateTimeProperty()
  author_name = db.StringProperty()
  author_url = db.LinkProperty()
  content = db.TextProperty()

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

    logging.debug('New comment to propagate! %s %r\n%s on %s',
                  self.kind(), self.key().id_or_name(),
                  self.source_comment_url, self.dest_post_url)
    taskqueue.add(queue_name='propagate', params={'comment_key': str(self.key())})
    self.save()
    return self


class DisableSource(Exception):
  """Raised when a user has deauthorized our app inside a given platform.
  """
