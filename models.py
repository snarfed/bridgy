"""Datastore model classes.

For the record, these are the remote_api_shell commands I used to do the schema
migration from Comment to Response. *First*, temporarily drop the auto_now=True
on the 'updated' property. Then:

~/google_appengine/remote_api_shell.py -s localhost:8080
OR
~/google_appengine/remote_api_shell.py brid-gy

heaven@gmail.com
...

from models import Comment, Response

for c in Comment.all():
  props = db.to_dict(c)
  props['response_json'] = props.pop('comment_json')
  Response(key_name=c.key().name(), **props).save()

# sanity check
Comment.all().count()
Response.all().count()

Now, add auto_now=True back to the 'updated' property.
"""

import datetime
import itertools
import json
import logging
import urllib
import urlparse

import appengine_config
import util
from webutil.models import KeyNameModel

from google.appengine.api import mail
from google.appengine.api import taskqueue
from google.appengine.api import users
from google.appengine.ext import db


class Site(KeyNameModel):
  """A web site for a single entity, e.g. Facebook profile or WordPress blog.
  """

  # human-readable name for this site type. subclasses should override.
  DISPLAY_NAME = None
  # short name for this site type. used in URLs, ec.
  SHORT_NAME = None
  STATUSES = ('enabled', 'disabled', 'error')

  created = db.DateTimeProperty(auto_now_add=True, required=True)
  url = db.LinkProperty()
  status = db.StringProperty(choices=STATUSES, default='enabled')

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
      new_msg = "Updated %s. Refresh to see what's new!" % existing.label()
    else:
      logging.info('Added %s %s %s', new.label(), new.key().name(), new.key())
      new_msg = "Added %s. Refresh to see what we've found!" % new.label()
      mail.send_mail(sender='add@brid-gy.appspotmail.com',
                     to='webmaster@brid.gy',
                     subject='Added Brid.gy user: %s %s' %
                     (new.label(), new.key().name()),
                     body='%s/#%s' % (handler.request.host_url, new.dom_id()))

    handler.messages = set([urllib.quote_plus(new_msg)])

    # TODO: ugh, *all* of this should be transactional
    new.save()
    return new

  def dom_id(self):
    """Returns the DOM element id for this site."""
    return '%s-%s' % (self.DISPLAY_NAME, self.key().name())


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

  def label(self):
    """Human-readable label for this site."""
    return '%s: %s' % (self.DISPLAY_NAME, self.name)

  def get_post(self, id):
    """Returns a post from this source.

    Args:
      id: string, site-specific post id
      fetch_replies: boolean, if True does any extra API calls needed to fetch
        replies/comments

    Returns: dict, decoded ActivityStreams activity, or None
    """
    activities = self.get_activities(activity_id=id)
    return activities[0] if activities else None

  def get_comment(self, comment_id, activity_id=None):
    """Returns a comment from this source.

    To be implemented by subclasses.

    Args:
      comment_id: string, site-specific comment id
      activity_id: string, site-specific activity id

    Returns: dict, decoded ActivityStreams comment object, or None
    """
    return self.as_source.get_comment(comment_id, activity_id=activity_id)

  def get_activities(self, fetch_replies=False, **kwargs):
    """Returns recent posts and embedded comments for this source.

    To be implemented by subclasses. Keyword args should be passed through to
    activitystreams-unofficial's Source.get_activities().

    Returns: list of dicts, decoded JSON ActivityStreams activity objects
      with comments in the 'replies' field, if any
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


class Response(KeyNameModel):
  """A comment, like, or repost to be propagated.

  The key name is the commentobject id as a tag URI.
  """
  TYPES = ('comment', 'like', 'repost')
  STATUSES = ('new', 'processing', 'complete', 'error')

  # ActivityStreams JSON activity and comment, like, or repost
  type = db.StringProperty(choices=TYPES, default='comment')
  activity_json = db.TextProperty()
  response_json = db.TextProperty()
  source = db.ReferenceProperty()
  status = db.StringProperty(choices=STATUSES, default='new')
  leased_until = db.DateTimeProperty()
  updated = db.DateTimeProperty(auto_now=True)

  # Original post links, ie webmention targets
  sent = db.StringListProperty()
  unsent = db.StringListProperty()
  error = db.StringListProperty()

  @db.transactional
  def get_or_save(self):
    existing = db.get(self.key())
    if existing:
      # logging.debug('Deferring to existing response %s.', existing.key().name())
      # this might be a nice sanity check, but we'd need to hard code certain
      # properties (e.g. content) so others (e.g. status) aren't checked.
      # for prop in self.properties().values():
      #   new = prop.get_value_for_datastore(self)
      #   existing = prop.get_value_for_datastore(existing)
      #   assert new == existing, '%s: new %s, existing %s' % (prop, new, existing)
      return existing

    obj = json.loads(self.response_json)
    type = obj.get('objectType')
    if type == 'activity':
      type = obj.get('verb')
    # default to comment. (e.g. Twitter replies technically have objectType note)
    self.type = type if type in ('like', 'repost') else 'comment'

    logging.debug('New response to propagate! %s %r %s', self.type,
                  self.key().id_or_name(), obj.get('url', obj.get('id')))
    taskqueue.add(queue_name='propagate', params={'response_key': str(self.key())})
    self.save()
    return self


class Comment(Response):
  """Backward compatibility. TODO: remove.
  """
  comment_json = db.TextProperty()


class DisableSource(Exception):
  """Raised when a user has deauthorized our app inside a given platform.
  """
