"""Unit tests for tasks.py.
"""
import copy
import datetime

from granary import mastodon as gr_mastodon
from granary import twitter as gr_twitter
from granary.tests import test_flickr
from granary.tests import test_mastodon
import oauth_dropins.flickr
import oauth_dropins.flickr_auth
from oauth_dropins import indieauth
import oauth_dropins.mastodon
import oauth_dropins.twitter
import oauth_dropins.twitter_auth
from oauth_dropins.webutil.util import json_dumps, json_loads
import requests
from urllib3.exceptions import NewConnectionError

import cron
from flickr import Flickr
from mastodon import Mastodon
import models
from . import testutil
from .testutil import FakeSource
from twitter import Twitter
import tasks
import util


class CronTest(testutil.BackgroundTest):
  def setUp(self):
    super().setUp()
    oauth_dropins.flickr_auth.FLICKR_APP_KEY = 'my_app_key'
    oauth_dropins.flickr_auth.FLICKR_APP_SECRET = 'my_app_secret'
    oauth_dropins.twitter_auth.TWITTER_APP_KEY = 'my_app_key'
    oauth_dropins.twitter_auth.TWITTER_APP_SECRET = 'my_app_secret'

  def test_replace_poll_tasks(self):
    now = util.now_fn()

    # a bunch of sources, one needs a new poll task
    five_min_ago = now - datetime.timedelta(minutes=5)
    day_and_half_ago = now - datetime.timedelta(hours=36)
    month_ago = now - datetime.timedelta(days=30)
    defaults = {
      'features': ['listen'],
      'last_webmention_sent': day_and_half_ago,
      }

    self.clear_datastore()
    sources = [
      # doesn't need a new poll task
      FakeSource.new(last_poll_attempt=now, **defaults).put(),
      FakeSource.new(last_poll_attempt=five_min_ago, **defaults).put(),
      FakeSource.new(status='disabled', **defaults).put(),
      FakeSource.new(status='disabled', **defaults).put(),
      # need a new poll task
      FakeSource.new(status='enabled', **defaults).put(),
      # not signed up for listen
      FakeSource.new(last_webmention_sent=day_and_half_ago).put(),
      # never sent a webmention, past grace period. last polled is older than 2x
      # fast poll, but within 2x slow poll.
      FakeSource.new(features=['listen'], created=month_ago,
                     last_poll_attempt=day_and_half_ago).put(),
      ]

    self.expect_task('poll', source_key=sources[4], last_polled='1970-01-01-00-00-00')
    self.mox.ReplayAll()

    resp = self.client.get('/cron/replace_poll_tasks')
    self.assertEqual(200, resp.status_code)

  def test_update_twitter_pictures(self):
    sources = []
    for screen_name in ('a', 'b', 'c'):
      auth_entity = oauth_dropins.twitter.TwitterAuth(
        id='id', token_key='key', token_secret='secret',
        user_json=json_dumps({'name': 'Ryan',
                              'screen_name': screen_name,
                              'profile_image_url': 'http://pi.ct/ure',
                              }))
      auth_entity.put()
      sources.append(Twitter.new(auth_entity=auth_entity, features=['listen']).put())

    user_obj = {
      'screen_name': sources[1].id(),
      'profile_image_url_https': 'http://new/pic_normal.jpg',
      'profile_image_url': 'http://bad/http',
    }

    lookup_url = gr_twitter.API_BASE + gr_twitter.API_USER
    self.expect_urlopen(lookup_url % 'a', json_dumps(user_obj))
    self.expect_urlopen(lookup_url % 'b', json_dumps(user_obj))
    self.expect_urlopen(lookup_url % 'c', json_dumps(user_obj))
    self.mox.ReplayAll()

    resp = self.client.get('/cron/update_twitter_pictures')
    self.assertEqual(200, resp.status_code)

    for source in sources:
      self.assertEqual('http://new/pic.jpg', source.get().picture)

  def test_update_twitter_picture_user_lookup_404s(self):
    auth_entity = oauth_dropins.twitter.TwitterAuth(
      id='id', token_key='key', token_secret='secret',
      user_json=json_dumps({'name': 'Bad',
                            'screen_name': 'bad',
                            'profile_image_url': 'http://pi.ct/ure',
                           }))
    auth_entity.put()
    source = Twitter.new(auth_entity=auth_entity, features=['publish']).put()

    lookup_url = gr_twitter.API_BASE + gr_twitter.API_USER
    self.expect_urlopen(lookup_url % 'bad', status=404)
    self.mox.ReplayAll()

    resp = self.client.get('/cron/update_twitter_pictures')
    self.assertEqual(200, resp.status_code)

    self.assertEqual('http://pi.ct/ure', source.get().picture)

  def test_update_flickr_pictures(self):
    flickrs = self._setup_flickr()

    self.mox.StubOutWithMock(cron, 'PAGE_SIZE')
    cron.PAGE_SIZE = 1

    # first
    self.expect_urlopen(
      'https://api.flickr.com/services/rest?nojsoncallback=1&format=json&method=flickr.people.getInfo&user_id=123%40N00',
      json_dumps({
        'person': {
          'id': '789@N99',
          'nsid': '789@N99',
          'iconfarm': 9,
          'iconserver': '9876',
        }}))
    # second has no features, gets skipped
    self.mox.ReplayAll()

    # first
    self.assertEqual(
      'https://farm5.staticflickr.com/4068/buddyicons/123@N00.jpg',
      flickrs[0].picture)

    resp = self.client.get('/cron/update_flickr_pictures')
    self.assertEqual(200, resp.status_code)
    self.assertEqual(
      'https://farm9.staticflickr.com/9876/buddyicons/789@N99.jpg',
      flickrs[0].key.get().picture)

    cursor = cron.LastUpdatedPicture.get_by_id('flickr')
    self.assertEqual(flickrs[0].key, cursor.last)

    # second
    resp = self.client.get('/cron/update_flickr_pictures')
    self.assertEqual(200, resp.status_code)
    # unchanged
    self.assertEqual(flickrs[1].picture, flickrs[1].key.get().picture)

    cursor = cron.LastUpdatedPicture.get_by_id('flickr')
    # this would be None on prod, but the datastore emulator always returns
    # more=True even when there aren't more results. :(
    # https://github.com/googleapis/python-ndb/issues/241
    self.assertEqual(flickrs[1].key, cursor.last)

  def test_update_mastodon_pictures(self):
    self.expect_requests_get(
      'https://foo.com' + test_mastodon.API_ACCOUNT % 123,
      test_mastodon.ACCOUNT, headers={'Authorization': 'Bearer towkin'})
    self.mox.ReplayAll()

    mastodon = self._setup_mastodon()
    resp = self.client.get('/cron/update_mastodon_pictures')
    self.assertEqual(200, resp.status_code)
    self.assertEqual(test_mastodon.ACCOUNT['avatar'], mastodon.key.get().picture)

  def test_update_mastodon_pictures_get_actor_404(self):
    self.expect_requests_get(
      'https://foo.com' + test_mastodon.API_ACCOUNT % 123,
      headers={'Authorization': 'Bearer towkin'},
    ).AndRaise(
      requests.exceptions.HTTPError(
        response=util.Struct(status_code='404', text='foo')))
    self.mox.ReplayAll()

    mastodon = self._setup_mastodon()
    resp = self.client.get('/cron/update_mastodon_pictures')
    self.assertEqual(200, resp.status_code)
    self.assertEqual('http://before', mastodon.key.get().picture)

  def test_update_mastodon_pictures_get_actor_connection_failure(self):
    self.expect_requests_get(
      'https://foo.com' + test_mastodon.API_ACCOUNT % 123,
      headers={'Authorization': 'Bearer towkin'},
    ).AndRaise(NewConnectionError(None, None))
    self.mox.ReplayAll()

    mastodon = self._setup_mastodon()
    resp = self.client.get('/cron/update_mastodon_pictures')
    self.assertEqual(200, resp.status_code)
    self.assertEqual('http://before', mastodon.key.get().picture)

  def _setup_flickr(self):
    """Creates and test :class:`Flickr` entities."""
    flickrs = []

    for id, features in (('123@N00', ['listen']), ('456@N11', [])):
      info = copy.deepcopy(test_flickr.PERSON_INFO)
      info['person']['nsid'] = id
      flickr_auth = oauth_dropins.flickr.FlickrAuth(
        id=id, user_json=json_dumps(info),
        token_key='my_key', token_secret='my_secret')
      flickr_auth.put()
      flickr = Flickr.new(auth_entity=flickr_auth, features=features)
      flickr.put()
      flickrs.append(flickr)

    return flickrs

  def _setup_mastodon(self):
    """Creates and returns a test :class:`Mastodon`."""
    app = oauth_dropins.mastodon.MastodonApp(instance='https://foo.com', data='')
    app.put()
    auth = oauth_dropins.mastodon.MastodonAuth(
      id='@me@foo.com', access_token_str='towkin', app=app.key,
      user_json=json_dumps({
        'id': 123,
        'username': 'me',
        'acct': 'me',
        'avatar': 'http://before',
      }))
    auth.put()
    mastodon = Mastodon.new(auth_entity=auth, features=['listen'])
    mastodon.put()
    return mastodon
