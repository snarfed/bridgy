"""Twitter source code and datastore model classes.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import json
import logging
import re

import webapp2

import appengine_config

from activitystreams import twitter as as_twitter
from activitystreams.oauth_dropins import twitter as oauth_twitter
import models
import util


class Twitter(models.Source):
  """A Twitter account.

  The key name is the username.
  """

  AS_CLASS = as_twitter.Twitter
  SHORT_NAME = 'twitter'

  # Twitter's rate limiting window is currently 15m. We handle replies,
  # retweets, and favorites in twitter_streaming anyway, so this is mainly just
  # for backup.
  # https://dev.twitter.com/docs/rate-limiting/1.1/limits
  POLL_FREQUENCY = datetime.timedelta(minutes=20)

  @staticmethod
  def new(handler, auth_entity=None):
    """Creates and returns a Twitter entity.

    Args:
      handler: the current RequestHandler
    """
    user = json.loads(auth_entity.user_json)
    # use https picture url if available, and drop the '_normal' suffix, which
    # gives us a higher res image, ~256x256 instead of ~48x48.
    picture = user.get('profile_image_url_https') or user.get('profile_image_url')
    picture = picture.replace('_normal.', '.', 1)
    return Twitter(id=user['screen_name'],
                   auth_entity=auth_entity.key,
                   url=Twitter.user_url(user['screen_name']),
                   name=user['name'],
                   picture=picture)

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
    resp = models.Response.get_by_id(id)
    return json.loads(resp.response_json) if resp else None

  @staticmethod
  def user_url(username):
    """Returns a user's URL.
    """
    return 'http://twitter.com/%s' % username


class AddTwitter(oauth_twitter.CallbackHandler, util.Handler):
  def finish(self, auth_entity, state=None):
    if not auth_entity:
      self.messages.add("OK, you're not signed up. Hope you reconsider!")
      self.redirect('/')
      return

    tw = Twitter.create_new(self, auth_entity=auth_entity)
    util.added_source_redirect(self, tw)


application = webapp2.WSGIApplication([
    ('/twitter/start', oauth_twitter.StartHandler.to('/twitter/add')),
    ('/twitter/add', AddTwitter),
    ('/twitter/delete/finish', oauth_twitter.CallbackHandler.to('/delete/finish')),
    ], debug=appengine_config.DEBUG)
