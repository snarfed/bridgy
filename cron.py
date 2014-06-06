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
USERS_PER_LOOKUP = 100  # max # of users per API call


class ReplacePollTasks(webapp2.RequestHandler):
  """Finds sources missing their poll tasks and adds new ones."""

  def get(self):
    now = datetime.datetime.now()
    queries = [cls.query(Source.features == 'listen', Source.status == 'enabled')
               for cls in handlers.SOURCES.values()]
    for source in itertools.chain(*queries):
      age = now - source.last_polled
      if age > source.poll_period() * 4:
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
    usernames = sources.keys()
    users = []
    for i in range(0, len(usernames), USERS_PER_LOOKUP):
      url = TWITTER_API_USER_LOOKUP % ','.join(usernames[i:i + USERS_PER_LOOKUP])
      users += json.loads(auther.as_source.urlopen(url).read())

    for user in users:
      source = sources[user['screen_name']]
      new_actor = auther.as_source.user_to_actor(user)
      new_pic = new_actor.get('image', {}).get('url')
      if source.picture != new_pic:
        logging.info('Updating profile picture for %s from %s to %s',
                     source.bridgy_url(self), source.picture, new_pic)
        util.CachedPage.invalidate('/users')
        source.picture = new_pic
        source.put()


application = webapp2.WSGIApplication([
    ('/cron/replace_poll_tasks', ReplacePollTasks),
    ('/cron/update_twitter_pictures', UpdateTwitterPictures),
    ], debug=appengine_config.DEBUG)
