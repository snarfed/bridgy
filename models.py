"""Datastore model classes.
"""

import datetime
import logging

import appengine_config
import util

from google.appengine.api import users
from google.appengine.ext import db
from google.appengine.ext import webapp
from google.appengine.ext.webapp.util import run_wsgi_app


class Site(util.KeyNameModel):
  """A web site for a single entity, e.g. Facebook profile or Wordpress blog.

  Not intended to be used directly. Inherit from one or both of the Destination
  and Source subclasses.
  """
  created = db.DateTimeProperty(auto_now_add=True, required=True)
  url = db.LinkProperty()

  def display_name(self):
    """Returns a human-readable name for this site, e.g. 'My Thoughts'.
    
    To be implemented by subclasses.
    """
    raise NotImplementedError()

  def type_display_name(self):
    """Returns a human-readable name for this type of site, e.g. 'Facebook'.
    
    To be implemented by subclasses.
    """
    raise NotImplementedError()

  # last_polled = db.DateTimeProperty()
  # destinations = db.StringListProperty(choices=DESTINATIONS)


class Source(Site):
  """A web site to read comments from, e.g. a Facebook profile.

  Each concrete source class should subclass this class.
  """

  last_polled = db.DateTimeProperty(default=util.EPOCH)

  def poll(self):
    """Returns a list of comments from this source.

    To be implemented by subclasses. The returned list should have Comment
    entities in increasing timestamp order.
    """
    raise NotImplementedError()


class Destination(Site):
  """A web site to propagate comments to, e.g. a Wordpress blog.

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
  dest = db.ReferenceProperty(reference_class=Destination)#, required=True)
  created = db.DateTimeProperty()
  source_post_url = db.LinkProperty()
  source_comment_url = db.LinkProperty()
  dest_post_url = db.LinkProperty()
  dest_comment_url = db.LinkProperty()

  author_name = db.StringProperty()
  author_url = db.LinkProperty()
  content = db.StringProperty()

  status = db.StringProperty(choices=STATUSES, default='new')
  leased_until = db.DateTimeProperty()

  @db.transactional
  def get_or_save(self):
    existing = db.get(self.key())
    if existing:
      for prop in self.properties().values():
        assert (prop.get_value_for_datastore(self) ==
                prop.get_value_for_datastore(existing))
      return existing

    self.save()
    return self


class User(db.Model):
  """A registered user.

  The key name is either App Engine user_id or OpenID federated_identity.
  """

  sources = db.ListProperty(db.Key)
  dests = db.ListProperty(db.Key)

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
      they're not logged in, 
    """
    user = users.get_current_user()
    if user:
      return user.federated_identity() or user.user_id()

  @db.transactional
  def add_dest(self, dest):
    if dest.key() not in self.dests:
      self.dests.append(dest.key())
      self.save()

  @db.transactional
  def add_source(self, source):
    if source.key() not in self.sources:
      self.sources.append(source.key())
      self.save()
