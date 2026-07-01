"""Unit tests for tasks.py.
"""
import copy
import datetime
from unittest.mock import patch

from granary import mastodon as gr_mastodon
from granary.tests import test_flickr
from granary.tests import test_mastodon
import oauth_dropins.flickr
import oauth_dropins.flickr_auth
from oauth_dropins import indieauth
import oauth_dropins.mastodon
from webutil.testutil import requests_response, UrlopenResult
from webutil.util import json_dumps, json_loads
import requests
from urllib3.exceptions import NewConnectionError

import cron
from flickr import Flickr
from mastodon import Mastodon
import models
from . import testutil
from .testutil import FakeSource
import tasks
import util


class CronTest(testutil.BackgroundTest):
  def setUp(self):
    super().setUp()
    oauth_dropins.flickr_auth.FLICKR_APP_KEY = 'my_app_key'
    oauth_dropins.flickr_auth.FLICKR_APP_SECRET = 'my_app_secret'

  def test_replace_poll_tasks(self):
    now = util.now()

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

    resp = self.client.get('/cron/replace_poll_tasks')
    self.assertEqual(200, resp.status_code)
    self.assert_task('poll', source_key=sources[4], last_polled='1970-01-01-00-00-00')

  # @patch('cron.PAGE_SIZE', new=1)
  def test_update_flickr_pictures(self):
    flickrs = self._setup_flickr()

    # first
    self.mock_urlopen.return_value = UrlopenResult(200, json_dumps({
      'person': {
        'id': '789@N99',
        'nsid': '789@N99',
        'iconfarm': 9,
        'iconserver': '9876',
      }}))
    # second has no features, gets skipped

    # first
    self.assertEqual(
      'https://farm5.staticflickr.com/4068/buddyicons/123@N00.jpg',
      flickrs[0].picture)

    resp = self.client.get('/cron/update_flickr_pictures')
    self.assertEqual(200, resp.status_code)
    self.assertEqual(
      'https://farm9.staticflickr.com/9876/buddyicons/789@N99.jpg',
      flickrs[0].key.get().picture)
    self.assert_urlopen(
      'https://api.flickr.com/services/rest?nojsoncallback=1&format=json&method=flickr.people.getInfo&user_id=123%40N00')

    cursor = cron.LastUpdatedPicture.get_by_id('flickr')

    # second
    resp = self.client.get('/cron/update_flickr_pictures')
    self.assertEqual(200, resp.status_code)
    # unchanged
    self.assertEqual(flickrs[1].picture, flickrs[1].key.get().picture)

    cursor = cron.LastUpdatedPicture.get_by_id('flickr')
    self.assertIsNone(cursor.last)

  def test_update_mastodon_pictures(self):
    self.mock_get.return_value = requests_response(
      test_mastodon.ACCOUNT, content_type='application/json')

    mastodon = self._setup_mastodon()
    resp = self.client.get('/cron/update_mastodon_pictures')
    self.assertEqual(200, resp.status_code)
    self.assertEqual(test_mastodon.ACCOUNT['avatar'], mastodon.key.get().picture)
    self.assert_requests_get('https://foo.com' + gr_mastodon.API_ACCOUNT % 123)

  def test_update_mastodon_pictures_get_actor_404(self):
    self.mock_get.side_effect = requests.exceptions.HTTPError(
      response=util.Struct(status_code='404', text='foo'))

    mastodon = self._setup_mastodon()
    resp = self.client.get('/cron/update_mastodon_pictures')
    self.assertEqual(200, resp.status_code)
    self.assertEqual('http://before', mastodon.key.get().picture)

  def test_update_mastodon_pictures_get_actor_connection_failure(self):
    self.mock_get.side_effect = NewConnectionError(None, None)

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
