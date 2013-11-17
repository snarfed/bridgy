"""Instagram API code and datastore model classes.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import collections
import datetime
import json
import logging
import pprint
import urllib
import urlparse

from activitystreams.oauth_dropins import instagram as oauth_instagram
import appengine_config
import models
import util

from google.appengine.api import urlfetch
from google.appengine.ext import db
import webapp2


class Instagram(models.Source):
  """A instagram account.

  The key name is the username.
  """

  TYPE_NAME = 'Instagram'

  def display_name(self):
    return self.name

  @staticmethod
  def new(handler, auth_entity=None):
    """Creates and returns a InstagramPage for the logged in user.

    Args:
      handler: the current RequestHandler
      auth_entity: oauth_dropins.instagram.InstagramAuth
    """
    user = json.loads(auth_entity.user_json)
    username = user['username']
    return Instagram(key_name=username,
                     owner=models.User.get_current_user(),
                     auth_entity=auth_entity,
                     name=user['full_name'],
                     picture=user['profile_picture'],
                     url='http://instagram.com/' + username)

  def get_posts(self):
    """Returns list of (link id aka post object id, link url).
    """
    raise NotImplementedError()

  def get_comments(self, posts_and_targets):
    raise NotImplementedError()


class InstagramComment(models.Comment):
  """Key name is the comment's object_id.

  Most of the properties correspond to the columns of the content table in FQL.
  http://developers.instagram.com/docs/reference/fql/comment/
  """
  # user id who wrote the comment
  from_username = db.IntegerProperty(required=True)

  # id of the object this comment refers to
  object_id = db.IntegerProperty(required=True)


class AddInstagram(oauth_instagram.CallbackHandler):
  messages = []

  def finish(self, auth_entity, state=None):
    Instagram.create_new(self, auth_entity=auth_entity)
    self.redirect('/')


application = webapp2.WSGIApplication([
    ('/instagram/start', oauth_instagram.StartHandler.to('/instagram/oauth_callback')),
    ('/instagram/oauth_callback', AddInstagram),
    ], debug=appengine_config.DEBUG)
