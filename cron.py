"""Cron jobs. Currently just minor cleanup tasks.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import itertools
import json
import logging

import appengine_config

import handlers
from models import Source
from twitter import Twitter
import util
import webapp2

from google.appengine.api import memcache

TWITTER_API_USER_LOOKUP = 'https://api.twitter.com/1.1/users/lookup.json?screen_name=%s'
TOO_OLD = datetime.timedelta(hours=2)


class ReplacePollTasks(webapp2.RequestHandler):
  """Finds sources missing their poll tasks and adds new ones."""

  def get(self):
    now = datetime.datetime.now()
    queries = [cls.query(Source.features == 'listen', Source.status == 'enabled')
               for cls in handlers.SOURCES.values()]
    for source in itertools.chain(*queries):
      age = now - source.last_polled
      if age > TOO_OLD:
        logging.info('%s last polled %s ago. Adding new poll task.',
                     source.bridgy_url(self), age)
        util.add_poll_task(source)


class UpdateTwitterPictures(webapp2.RequestHandler):
  """Finds Twitter sources whose profile pictures have changed and updates them."""

  def get(self):
    sources = {source.key.id(): source for source in Twitter.query()}
    if not sources:
      return

    # just auth as me or the first user. TODO: use app-ony auth instead.
    auther = sources.get('schnarfed') or sources.values()[0]
    users = auther.as_source.urlopen(
      TWITTER_API_USER_LOOKUP % ','.join(sources))

    for user in json.loads(users.read()):
      source = sources[user['screen_name']]
      new_pic = Twitter.get_picture(user)
      if source.picture != new_pic:
        logging.info('Updating profile picture for %s from %s to %s',
                     source.bridgy_url(self), source.picture, new_pic)
        util.CachedFrontPage.invalidate()
        source.picture = new_pic
        source.put()


application = webapp2.WSGIApplication([
    ('/cron/replace_poll_tasks', ReplacePollTasks),
    ('/cron/update_twitter_pictures', UpdateTwitterPictures),
    ], debug=appengine_config.DEBUG)
