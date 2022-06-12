"""Cron jobs. Currently just minor cleanup tasks.
"""
from builtins import range
from datetime import datetime, timedelta, timezone
import itertools
import logging

from flask import g
from flask.views import View
from google.cloud import ndb
from oauth_dropins.webutil.models import StringIdModel
import requests

from blogger import Blogger
from flask_background import app
from flickr import Flickr
from mastodon import Mastodon
from reddit import Reddit
import models
from models import Source
from twitter import Twitter
import util

logger = logging.getLogger(__name__)

CIRCLECI_TOKEN = util.read('circleci_token')
PAGE_SIZE = 20


class LastUpdatedPicture(StringIdModel):
  """Stores the last user in a given silo that we updated profile picture for.

  Key id is the silo's SHORT_NAME.
  """
  last = ndb.KeyProperty()
  created = ndb.DateTimeProperty(auto_now_add=True, required=True, tzinfo=timezone.utc)
  updated = ndb.DateTimeProperty(auto_now=True, tzinfo=timezone.utc)


@app.route('/cron/replace_poll_tasks')
def replace_poll_tasks():
  """Finds sources missing their poll tasks and adds new ones."""
  now = util.now_fn()
  fields = [cls.last_polled, cls.last_poll_attempt, cls.rate_limited]
  queries = [cls.query(Source.features == 'listen', Source.status == 'enabled',
                       projection=fields)
             for cls in models.sources.values() if cls.AUTO_POLL]
  for source in itertools.chain(*queries):
    age = now - source.last_poll_attempt
    if age > max(source.poll_period() * 2, timedelta(days=2)):
      logger.info(f'{source.bridgy_url()} last polled {age} ago. Adding new poll task.')
      util.add_poll_task(source)

  return ''


class UpdatePictures(View):
  """Finds sources with new profile pictures and updates them."""
  SOURCE_CLS = None

  @classmethod
  def user_id(cls, source):
    return source.key_id()

  def dispatch_request(self):
    g.TRANSIENT_ERROR_HTTP_CODES = (self.SOURCE_CLS.TRANSIENT_ERROR_HTTP_CODES +
                                    self.SOURCE_CLS.RATE_LIMIT_HTTP_CODES)

    query = self.SOURCE_CLS.query().order(self.SOURCE_CLS.key)
    last = LastUpdatedPicture.get_by_id(self.SOURCE_CLS.SHORT_NAME)
    if last and last.last:
      query = query.filter(self.SOURCE_CLS.key > last.last)

    results, _, more = query.fetch_page(PAGE_SIZE)
    for source in results:
      if source.features and source.status != 'disabled':
        user_id = self.user_id(source)
        logger.debug(f'checking for updated profile pictures for {source.bridgy_url()} {user_id}')
        try:
          actor = source.gr_source.get_actor(user_id)
        except BaseException as e:
          logging.debug('Failed', exc_info=True)
          # Mastodon API returns HTTP 404 for deleted (etc) users, and
          # often one or more users' Mastodon instances are down.
          code, _ = util.interpret_http_exception(e)
          if code:
            continue
          raise

        if not actor:
          logger.info(f"Couldn't fetch user")
          continue

        new_pic = actor.get('image', {}).get('url')
        if not new_pic or source.picture == new_pic:
          logger.info(f'No new picture found')
          continue

        @ndb.transactional()
        def update():
          src = source.key.get()
          src.picture = new_pic
          src.put()

        logger.info(f'Updating profile picture from {source.picture} to {new_pic}')
        update()

    LastUpdatedPicture(id=self.SOURCE_CLS.SHORT_NAME,
                       last=source.key if more else None).put()
    return 'OK'


class UpdateFlickrPictures(UpdatePictures):
  """Finds :class:`Flickr` sources with new profile pictures and updates them."""
  SOURCE_CLS = Flickr


class UpdateMastodonPictures(UpdatePictures):
  """Finds :class:`Mastodon` sources with new profile pictures and updates them."""
  SOURCE_CLS = Mastodon

  @classmethod
  def user_id(cls, source):
    return source.auth_entity.get().user_id()


class UpdateTwitterPictures(UpdatePictures):
  """Finds :class:`Twitter` sources with new profile pictures and updates them.

  https://github.com/snarfed/granary/commit/dfc3d406a20965a5ed14c9705e3d3c2223c8c3ff
  http://indiewebcamp.com/Twitter#Profile_Image_URLs
  """
  SOURCE_CLS = Twitter


class UpdateRedditPictures(UpdatePictures):
  """Finds :class:`Reddit` sources with new profile pictures and updates them."""
  SOURCE_CLS = Reddit


app.add_url_rule('/cron/update_flickr_pictures',
                 view_func=UpdateFlickrPictures.as_view('update_flickr_pictures'))
app.add_url_rule('/cron/update_mastodon_pictures',
                 view_func=UpdateMastodonPictures.as_view('update_mastodon_pictures'))
app.add_url_rule('/cron/update_reddit_pictures',
                 view_func=UpdateRedditPictures.as_view('update_reddit_pictures'))
app.add_url_rule('/cron/update_twitter_pictures',
                 view_func=UpdateTwitterPictures.as_view('update_twitter_pictures'))
