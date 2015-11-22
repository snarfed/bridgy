"""Cron jobs. Currently just minor cleanup tasks.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import itertools
import json
import logging

from google.appengine.ext import ndb
import appengine_config

import models
from models import Source
from instagram import Instagram
from twitter import Twitter
import util
import webapp2

TWITTER_API_USER_LOOKUP = 'users/lookup.json?screen_name=%s'
TWITTER_USERS_PER_LOOKUP = 100  # max # of users per API call


class ReplacePollTasks(webapp2.RequestHandler):
  """Finds sources missing their poll tasks and adds new ones."""

  def get(self):
    now = datetime.datetime.now()
    queries = [cls.query(Source.features == 'listen', Source.status == 'enabled')
               for cls in models.sources.values()]
    for source in itertools.chain(*queries):
      age = now - source.last_poll_attempt
      if age > max(source.poll_period() * 2, datetime.timedelta(hours=2)):
        logging.info('%s last polled %s ago. Adding new poll task.',
                     source.bridgy_url(self), age)
        util.add_poll_task(source)


class UpdateTwitterPictures(webapp2.RequestHandler):
  """Finds Twitter sources whose profile pictures have changed and updates them.

  *This is currently unused!!!* We switched to the unofficial "evergreen"
  Twitter profile picture URLs, https://twitter.com/USER/profile_image , which
  always redirect to the current image.

  https://github.com/snarfed/granary/commit/dfc3d406a20965a5ed14c9705e3d3c2223c8c3ff
  http://indiewebcamp.com/Twitter#Profile_Image_URLs
  """

  def get(self):
    sources = {source.key.id(): source for source in Twitter.query()}
    if not sources:
      return

    # just auth as me or the first user. TODO: use app-only auth instead.
    auther = sources.get('schnarfed') or sources.values()[0]
    usernames = sources.keys()
    users = []
    for i in range(0, len(usernames), TWITTER_USERS_PER_LOOKUP):
      url = TWITTER_API_USER_LOOKUP % ','.join(
        usernames[i:i + TWITTER_USERS_PER_LOOKUP])
      users += auther.gr_source.urlopen(url)

    for user in users:
      source = sources[user['screen_name']]
      new_actor = auther.gr_source.user_to_actor(user)
      maybe_update_picture(source, new_actor, self)


class UpdateInstagramPictures(webapp2.RequestHandler):
  """Finds Instagram sources whose profile pictures have changed and updates them."""

  def get(self):
    for source in Instagram.query():
      if source.features and source.status != 'disabled':
        maybe_update_picture(source, source.gr_source.get_actor(), self)


def maybe_update_picture(source, new_actor, handler):
  new_pic = new_actor.get('image', {}).get('url')
  if not new_pic or source.picture == new_pic:
    return

  @ndb.transactional
  def update():
    src = source.key.get()
    src.picture = new_pic
    src.put()

  logging.info('Updating profile picture for %s from %s to %s',
               source.bridgy_url(handler), source.picture, new_pic)
  update()
  util.CachedPage.invalidate('/users')


application = webapp2.WSGIApplication([
    ('/cron/replace_poll_tasks', ReplacePollTasks),
    ('/cron/update_twitter_pictures', UpdateTwitterPictures),
    ('/cron/update_instagram_pictures', UpdateInstagramPictures),
    ], debug=appengine_config.DEBUG)
