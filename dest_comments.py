"""bridgy App Engine app.

Datastore model classes for destination comments.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

from google.appengine.ext import db

import destinations


class DestComment(object):
  """A propagated comment in a destination site.

  The key name is the comment's uid in the destination site, e.g. comment_id in
  Wordpress.

  A DestComment is a child of its corresponding SourceComment."""
  dest = db.ReferenceProperty(reference_class=destinations.Destination,
                              required=True)
  created = db.DateTimeProperty(auto_now_add=True, required=True)
  dest_url = db.LinkProperty()

  # def __init__(self, **kwargs):
  #   assert 'parent' in kwargs and kwargs['parent']


class WordpressDestComment(db.Model, DestComment):
  """A comment in facebook.

  The key name is the comment's post_fbid:
  http://developers.facebook.com/docs/reference/fql/comment/
  """
  pass
