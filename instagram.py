"""Instagram API code and datastore model classes.

Example post ID and links
  id: 595990791004231349 or 595990791004231349_247678460
    (suffix is user id)
  Permalink: http://instagram.com/p/hFYnd7Nha1/
  API URL: https://api.instagram.com/v1/media/595990791004231349
  Local handler path: /post/instagram/212038/595990791004231349

Example comment ID and links
  id: 595996024371549506
  No direct API URL or permalink, as far as I can tell. :/
  API URL for all comments on that picture:
    https://api.instagram.com/v1/media/595990791004231349_247678460/comments
  Local handler path:
    /comment/instagram/212038/595990791004231349_247678460/595996024371549506
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import json

from activitystreams import instagram as as_instagram
from activitystreams.oauth_dropins import instagram as oauth_instagram
from activitystreams.source import SELF
import appengine_config
import models

from google.appengine.ext import db
import webapp2


class Instagram(models.Source):
  """A instagram account.

  The key name is the username.
  """

  DISPLAY_NAME = 'Instagram'
  SHORT_NAME = 'instagram'

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

  def __init__(self, *args, **kwargs):
    super(Instagram, self).__init__(*args, **kwargs)
    if self.auth_entity:
      self.as_source = as_instagram.Instagram(self.auth_entity.access_token())

  def get_activities(self, fetch_replies=False, **kwargs):
    return self.as_source.get_activities(group_id=SELF, **kwargs)[1]


class AddInstagram(oauth_instagram.CallbackHandler):
  messages = []

  def finish(self, auth_entity, state=None):
    Instagram.create_new(self, auth_entity=auth_entity)
    self.redirect('/')


application = webapp2.WSGIApplication([
    ('/instagram/start', oauth_instagram.StartHandler.to('/instagram/oauth_callback')),
    ('/instagram/oauth_callback', AddInstagram),
    ], debug=appengine_config.DEBUG)
