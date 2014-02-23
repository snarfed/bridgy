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

import appengine_config

from activitystreams import instagram as as_instagram
from activitystreams.oauth_dropins import instagram as oauth_instagram
from activitystreams.source import SELF
import models
import util

from google.appengine.ext import ndb
import webapp2


class Instagram(models.Source):
  """A instagram account.

  The key name is the username.
  """

  AS_CLASS = as_instagram.Instagram
  SHORT_NAME = 'instagram'

  @staticmethod
  def new(handler, auth_entity=None, **kwargs):
    """Creates and returns a InstagramPage for the logged in user.

    Args:
      handler: the current RequestHandler
      auth_entity: oauth_dropins.instagram.InstagramAuth
    """
    user = json.loads(auth_entity.user_json)
    username = user['username']
    return Instagram(id=username,
                     auth_entity=auth_entity.key,
                     name=user['full_name'],
                     picture=user['profile_picture'],
                     url='http://instagram.com/' + username,
                     **kwargs)

  def get_activities_response(self, *args, **kwargs):
    """Discard min_id because we still want new comments/likes on old photos."""
    if 'min_id' in kwargs:
      del kwargs['min_id']
    return self.as_source.get_activities_response(*args, group_id=SELF, **kwargs)


class OAuthCallback(oauth_instagram.CallbackHandler, util.Handler):
  """OAuth callback handler.

  Both the add and delete flows have to share this because Instagram only allows
  a single callback URL per app. :/
  """

  def finish(self, auth_entity, state=None):
    self.maybe_add_or_delete_source(Instagram, auth_entity, state)


application = webapp2.WSGIApplication([
    ('/instagram/start', oauth_instagram.StartHandler.to('/instagram/oauth_callback')),
    ('/instagram/oauth_callback', OAuthCallback),
    ], debug=appengine_config.DEBUG)
