"""Twitter source code and datastore model classes.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import json
import logging
import re

from activitystreams import twitter as as_twitter
from activitystreams.oauth_dropins import twitter as oauth_twitter
from activitystreams.source import SELF
import appengine_config
import models
import util

import webapp2


class Twitter(models.Source):
  """A Twitter account.

  The key name is the username.
  """

  DISPLAY_NAME = 'Twitter'
  SHORT_NAME = 'twitter'

  @staticmethod
  def new(handler, auth_entity=None):
    """Creates and returns a Twitter entity.

    Args:
      handler: the current RequestHandler
    """
    user = json.loads(auth_entity.user_json)
    return Twitter(key_name=user['screen_name'],
                   auth_entity=auth_entity,
                   url=Twitter.user_url(user['screen_name']),
                   name=user['name'],
                   picture=user['profile_image_url'])

  def __init__(self, *args, **kwargs):
    super(Twitter, self).__init__(*args, **kwargs)
    if self.auth_entity:
      self.as_source = as_twitter.Twitter(*self.auth_entity.access_token())

  def get_like(self, activity_user_id, activity_id, like_user_id):
    """Returns an ActivityStreams 'like' activity object for a favorite.

    Twitter doesn't expose favorites in their REST API, so fetch it from the
    Response in the datastore.

    Args:
      activity_user_id: string id of the user who posted the original activity
      activity_id: string activity id
      like_user_id: string id of the user who liked the activity
    """
    id = self.as_source.tag_uri('%s_favorited_by_%s' % (activity_id, like_user_id))
    resp = models.Response.get_by_key_name(id)
    return json.loads(resp.response_json) if resp else None

  @staticmethod
  def user_url(username):
    """Returns a user's URL.
    """
    return 'http://twitter.com/%s' % username


class AddTwitter(oauth_twitter.CallbackHandler):
  messages = set()

  def finish(self, auth_entity, state=None):
    tw = Twitter.create_new(self, auth_entity=auth_entity)
    util.added_source_redirect(self, tw)


application = webapp2.WSGIApplication([
    ('/twitter/start', oauth_twitter.StartHandler.to('/twitter/add')),
    ('/twitter/add', AddTwitter),
    ('/twitter/delete/finish', oauth_twitter.CallbackHandler.to('/delete/finish')),
    ], debug=appengine_config.DEBUG)
