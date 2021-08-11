"""Cron jobs. Currently just minor cleanup tasks.
"""
from builtins import range
import datetime
import itertools
import logging
import math

from google.cloud import ndb
from oauth_dropins.webutil.util import json_dumps, json_loads
import requests
import webapp2

import models
from models import Source
from flickr import Flickr
from mastodon import Mastodon
from twitter import Twitter
import util

CIRCLECI_TOKEN = util.read('circleci_token')
TWITTER_API_USER_LOOKUP = 'users/lookup.json?screen_name=%s'
TWITTER_USERS_PER_LOOKUP = 100  # max # of users per API call


class ReplacePollTasks(webapp2.RequestHandler):
  """Finds sources missing their poll tasks and adds new ones."""
  handle_exception = util.background_handle_exception

  def get(self):
    now = datetime.datetime.now()
    queries = [cls.query(Source.features == 'listen', Source.status == 'enabled')
               for cls in models.sources.values() if cls.AUTO_POLL]
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
  TRANSIENT_ERROR_HTTP_CODES = (Twitter.TRANSIENT_ERROR_HTTP_CODES +
                                Twitter.RATE_LIMIT_HTTP_CODES)
  handle_exception = util.background_handle_exception

  def get(self):
    sources = {source.key_id(): source for source in Twitter.query()}
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

  handle_exception = util.background_handle_exception

  def source_query(self):
    return self.SOURCE_CLS.query()

  @classmethod
  def user_id(cls, source):
    return source.key_id()

  def get(self):
    updated = False
    for source in self.source_query():
      if source.features and source.status != 'disabled':
        logging.debug('checking for updated profile pictures for: %s',
                      source.bridgy_url(self))
        try:
          actor = source.gr_source.get_actor(self.user_id(source))
        except requests.HTTPError as e:
          # Mastodon API returns HTTP 404 for deleted (etc) users
          util.interpret_http_exception(e)
          continue
        updated = maybe_update_picture(source, actor, self)

    if updated:
      util.CachedPage.invalidate('/users')


class UpdateFlickrPictures(UpdatePictures):
  """Finds :class:`Flickr` sources with new profile pictures and updates them.
  """
  SOURCE_CLS = Flickr
  TRANSIENT_ERROR_HTTP_CODES = (Flickr.TRANSIENT_ERROR_HTTP_CODES +
                                Flickr.RATE_LIMIT_HTTP_CODES)


class UpdateMastodonPictures(UpdatePictures):
  """Finds :class:`Mastodon` sources with new profile pictures and updates them.
  """
  SOURCE_CLS = Mastodon
  TRANSIENT_ERROR_HTTP_CODES = (Mastodon.TRANSIENT_ERROR_HTTP_CODES +
                                Mastodon.RATE_LIMIT_HTTP_CODES)

  @classmethod
  def user_id(cls, source):
    return source.auth_entity.get().user_id()


def maybe_update_picture(source, new_actor, handler):
  if not new_actor:
    return False
  new_pic = new_actor.get('image', {}).get('url')
  if not new_pic or source.picture == new_pic:
    return False

  @ndb.transactional()
  def update():
    src = source.key.get()
    src.picture = new_pic
    src.put()

  logging.info('Updating profile picture for %s from %s to %s',
               source.bridgy_url(handler), source.picture, new_pic)
  update()
  return True


class BuildCircle(webapp2.RequestHandler):
  """Trigger CircleCI to build and test the main branch.

  ...to run twitter_live_test.py, to check that scraping likes is still working.
  """
  def get(self):
    resp = requests.post('https://circleci.com/api/v1.1/project/github/snarfed/bridgy/tree/main?circle-token=%s' % CIRCLECI_TOKEN)
    resp.raise_for_status()


# ROUTES = [
#   ('/cron/build_circle', BuildCircle),
#   ('/cron/replace_poll_tasks', ReplacePollTasks),
#   ('/cron/update_flickr_pictures', UpdateFlickrPictures),
#   ('/cron/update_mastodon_pictures', UpdateMastodonPictures),
#   ('/cron/update_twitter_pictures', UpdateTwitterPictures),
# ]
