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


class UpdatePictures(webapp2.RequestHandler):
  """Finds sources whose profile pictures have changed and
  updates them."""
  SOURCE_CLS = None

  def get(self):
    for source in self.SOURCE_CLS.query():
      logging.debug('checking source: %s', source)
      if source.features and source.status != 'disabled':
        maybe_update_picture(source, source.gr_source.get_actor(), self)


class UpdateInstagramPictures(UpdatePictures):
  """Finds Instagram sources whose profile pictures have changed and
  updates them."""
  SOURCE_CLS = Instagram


class UpdateFlickrPictures(UpdatePictures):
  """Finds Flickr sources whose profile pictures have changed and
  updates them."""
  SOURCE_CLS = Flickr


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
    ('/cron/update_instagram_pictures', UpdateInstagramPictures),
    ('/cron/update_flickr_pictures', UpdateFlickrPictures),
    ], debug=appengine_config.DEBUG)
