"""Cron jobs. Currently just minor cleanup tasks.
"""
from __future__ import unicode_literals
from __future__ import division

from future import standard_library
standard_library.install_aliases()
from builtins import range
import datetime
import itertools
import logging
import math

import appengine_config
from google.cloud import ndb
import http.client
from oauth_dropins.webutil.util import json_dumps, json_loads

import models
from models import Source
from instagram import Instagram
from twitter import Twitter
from flickr import Flickr
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
  """Finds :class:`Twitter` sources with new profile pictures and updates them.

  https://github.com/snarfed/granary/commit/dfc3d406a20965a5ed14c9705e3d3c2223c8c3ff
  http://indiewebcamp.com/Twitter#Profile_Image_URLs
  """

  def get(self):
    sources = {source.key.id(): source for source in Twitter.query()}
    if not sources:
      return

    # just auth as me or the first user. TODO: use app-only auth instead.
    auther = sources.get('schnarfed') or list(sources.values())[0]
    usernames = list(sources.keys())
    users = []
    for i in range(0, len(usernames), TWITTER_USERS_PER_LOOKUP):
      username_batch = usernames[i:i + TWITTER_USERS_PER_LOOKUP]
      url = TWITTER_API_USER_LOOKUP % ','.join(username_batch)
      try:
        users += auther.gr_source.urlopen(url)
      except Exception as e:
        code, body = util.interpret_http_exception(e)
        if not (code == '404' and len(username_batch) == 1):
          # 404 for a single user means they deleted their account. otherwise...
          raise

    updated = False
    for user in users:
      source = sources.get(user['screen_name'])
      if source:
        new_actor = auther.gr_source.user_to_actor(user)
        updated = maybe_update_picture(source, new_actor, self)

    if updated:
      util.CachedPage.invalidate('/users')


class UpdatePictures(webapp2.RequestHandler):
  """Finds sources with new profile pictures and updates them."""
  SOURCE_CLS = None

  def source_query(self):
    return self.SOURCE_CLS.query()

  def get(self):
    updated = False
    for source in self.source_query():
      logging.debug('checking for updated profile pictures for: %s',
                    source.bridgy_url(self))
      if source.features and source.status != 'disabled':
        updated = maybe_update_picture(
          source, source.gr_source.get_actor(source.key.id()), self)

    if updated:
      util.CachedPage.invalidate('/users')


class UpdateInstagramPictures(UpdatePictures):
  """Finds :class:`Instagram` sources with new profile pictures and updates them.

  Splits the accounts up into batches to avoid hitting Instagram's rate limit.
  Try to hit every account once a week.

  Testing on 2017-07-05 hit the rate limit after ~170 profile page requests,
  with ~270 total Instagram accounts on Bridgy.
  """
  SOURCE_CLS = Instagram
  FREQUENCY = datetime.timedelta(hours=1)
  WEEK = datetime.timedelta(days=7)
  BATCH = float(WEEK.total_seconds()) / FREQUENCY.total_seconds()

  def source_query(self):
    now = util.now_fn()
    since_sun = (now.weekday() * datetime.timedelta(days=1) +
                 (now - now.replace(hour=0, minute=0, second=0)))
    batch = float(Instagram.query().count()) / self.BATCH
    offset = batch * float(since_sun.total_seconds()) / self.FREQUENCY.total_seconds()
    return Instagram.query().fetch(offset=int(math.floor(offset)),
                                   limit=int(math.ceil(batch)))


class UpdateFlickrPictures(UpdatePictures):
  """Finds :class:`Flickr` sources with new profile pictures and updates them.
  """
  SOURCE_CLS = Flickr


def maybe_update_picture(source, new_actor, handler):
  if not new_actor:
    return False
  new_pic = new_actor.get('image', {}).get('url')
  if not new_pic or source.picture == new_pic:
    return False

  @ndb.transactional
  def update():
    src = source.key.get()
    src.picture = new_pic
    src.put()

  logging.info('Updating profile picture for %s from %s to %s',
               source.bridgy_url(handler), source.picture, new_pic)
  update()
  return True


application = webapp2.WSGIApplication([
    ('/cron/replace_poll_tasks', ReplacePollTasks),
    ('/cron/update_twitter_pictures', UpdateTwitterPictures),
    ('/cron/update_instagram_pictures', UpdateInstagramPictures),
    ('/cron/update_flickr_pictures', UpdateFlickrPictures),
], debug=appengine_config.DEBUG)
