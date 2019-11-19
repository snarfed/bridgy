"""Unit tests for tasks.py.
"""
from __future__ import unicode_literals
from __future__ import absolute_import

import copy
import datetime

from granary import instagram as gr_instagram
from granary import twitter as gr_twitter
from granary.tests import test_flickr
from granary.tests import test_instagram
import oauth_dropins
from oauth_dropins import indieauth
from oauth_dropins import flickr as oauth_flickr
from oauth_dropins import twitter as oauth_twitter
from oauth_dropins.webutil.util import json_dumps, json_loads

import appengine_config
import cron
from flickr import Flickr
from instagram import Instagram
from . import testutil
from .testutil import FakeSource, HandlerTest
from twitter import Twitter
import util


class CronTest(HandlerTest):
  def setUp(self):
    super(CronTest, self).setUp()
    oauth_dropins.appengine_config.FLICKR_APP_KEY = 'my_app_key'
    oauth_dropins.appengine_config.FLICKR_APP_SECRET = 'my_app_secret'
    oauth_dropins.appengine_config.TWITTER_APP_KEY = 'my_app_key'
    oauth_dropins.appengine_config.TWITTER_APP_SECRET = 'my_app_secret'

    flickr_auth = oauth_flickr.FlickrAuth(
      id='123@N00', user_json=json_dumps(test_flickr.PERSON_INFO),
      token_key='my_key', token_secret='my_secret')
    flickr_auth.put()
    self.flickr = Flickr.new(None, auth_entity=flickr_auth, features=['listen'])
    self.assertEquals(
      'https://farm5.staticflickr.com/4068/buddyicons/39216764@N00.jpg',
      self.flickr.picture)

  def setup_instagram(self, batch_size=None, weekday=0):
    self.mox.stubs.Set(appengine_config, 'INSTAGRAM_SESSIONID_COOKIE', None)
    if batch_size:
      self.mox.stubs.Set(cron.UpdateInstagramPictures, 'BATCH', batch_size)

    self.mox.StubOutWithMock(util, 'now_fn')
    # 2017-01-02 is a Monday, which datetime.weekday() returns 0 for
    util.now_fn().AndReturn(datetime.datetime(2017, 1, 2 + weekday))

  def expect_instagram_profile_fetch(self, username):
    profile = copy.deepcopy(test_instagram.HTML_PROFILE)
    profile['entry_data']['ProfilePage'][0]['graphql']['user'].update({
      'username': username,
      'profile_pic_url': 'http://new/pic',
    })
    super(HandlerTest, self).expect_requests_get(
      gr_instagram.HTML_BASE_URL + '%s/' % username,
      test_instagram.HTML_HEADER + json_dumps(profile) + test_instagram.HTML_FOOTER,
      allow_redirects=False)

  def test_replace_poll_tasks(self):
    self.assertEqual([], self.taskqueue_stub.GetTasks('poll'))
    now = datetime.datetime.now()

    # a bunch of sources, one needs a new poll task
    five_min_ago = now - datetime.timedelta(minutes=5)
    day_and_half_ago = now - datetime.timedelta(hours=36)
    month_ago = now - datetime.timedelta(days=30)
    defaults = {
      'features': ['listen'],
      'last_webmention_sent': day_and_half_ago,
      }
    sources = [
      # doesn't need a new poll task
      FakeSource.new(None, last_poll_attempt=now, **defaults).put(),
      FakeSource.new(None, last_poll_attempt=five_min_ago, **defaults).put(),
      FakeSource.new(None, status='disabled', **defaults).put(),
      FakeSource.new(None, status='disabled', **defaults).put(),
      # need a new poll task
      FakeSource.new(None, status='enabled', **defaults).put(),
      # not signed up for listen
      FakeSource.new(None, last_webmention_sent=day_and_half_ago).put(),
      # never sent a webmention, past grace period. last polled is older than 2x
      # fast poll, but within 2x slow poll.
      FakeSource.new(None, features=['listen'], created=month_ago,
                     last_poll_attempt=day_and_half_ago).put(),
      ]
    resp = cron.application.get_response('/cron/replace_poll_tasks')
    self.assertEqual(200, resp.status_int)

    tasks = self.taskqueue_stub.GetTasks('poll')
    self.assertEqual(1, len(tasks))
    self.assert_equals(sources[4].urlsafe(),
                       testutil.get_task_params(tasks[0])['source_key'])

  def test_update_twitter_pictures(self):
    sources = []
    for screen_name in ('a', 'b', 'c'):
      auth_entity = oauth_twitter.TwitterAuth(
        id='id', token_key='key', token_secret='secret',
        user_json=json_dumps({'name': 'Ryan',
                              'screen_name': screen_name,
                              'profile_image_url': 'http://pi.ct/ure',
                              }))
      auth_entity.put()
      sources.append(Twitter.new(None, auth_entity=auth_entity).put())

    user_objs = [{'screen_name': sources[0].id(),
                  'profile_image_url': 'http://pi.ct/ure',
                  }, {'screen_name': sources[1].id(),
                      'profile_image_url_https': 'http://new/pic_normal.jpg',
                      'profile_image_url': 'http://bad/http',
                  }]

    cron.TWITTER_USERS_PER_LOOKUP = 2
    lookup_url = gr_twitter.API_BASE + cron.TWITTER_API_USER_LOOKUP
    self.expect_urlopen(lookup_url % 'a,c', json_dumps(user_objs))
    self.expect_urlopen(lookup_url % 'b', json_dumps(user_objs))
    self.mox.ReplayAll()

    resp = cron.application.get_response('/cron/update_twitter_pictures')
    self.assertEqual(200, resp.status_int)

    self.assertEquals('http://pi.ct/ure', sources[0].get().picture)
    self.assertEquals('http://new/pic.jpg', sources[1].get().picture)

  def test_update_twitter_picture_user_lookup_404s(self):
    auth_entity = oauth_twitter.TwitterAuth(
      id='id', token_key='key', token_secret='secret',
      user_json=json_dumps({'name': 'Bad',
                            'screen_name': 'bad',
                            'profile_image_url': 'http://pi.ct/ure',
                           }))
    auth_entity.put()
    source = Twitter.new(None, auth_entity=auth_entity).put()

    lookup_url = gr_twitter.API_BASE + cron.TWITTER_API_USER_LOOKUP
    self.expect_urlopen(lookup_url % 'bad', status=404)
    self.mox.ReplayAll()

    resp = cron.application.get_response('/cron/update_twitter_pictures')
    self.assertEqual(200, resp.status_int)

    self.assertEquals('http://pi.ct/ure', source.get().picture)

  def test_update_instagram_pictures(self):
    self.setup_instagram(batch_size=1)
    for username in 'a', 'b':
      self.expect_instagram_profile_fetch(username)
    self.mox.ReplayAll()

    sources = []
    auth_entity = indieauth.IndieAuth(id='http://foo.com/', user_json='{}')
    for username in 'a', 'b', 'c', 'd':
      source = Instagram.new(
        None, auth_entity=auth_entity, features=['listen'],
        actor={'username': username, 'image': {'url': 'http://old/pic'}})
      # test that we skip disabled and deleted sources
      if username == 'c':
        source.status = 'disabled'
      elif username == 'd':
        source.features = []
      sources.append(source.put())

    resp = cron.application.get_response('/cron/update_instagram_pictures')
    self.assertEqual(200, resp.status_int)

    self.assertEquals('http://new/pic', sources[0].get().picture)
    self.assertEquals('http://new/pic', sources[1].get().picture)
    self.assertEquals('http://old/pic', sources[2].get().picture)
    self.assertEquals('http://old/pic', sources[3].get().picture)

  def test_update_instagram_pictures_batch(self):
    self.setup_instagram(weekday=3)
    self.expect_instagram_profile_fetch('d')
    self.mox.ReplayAll()

    sources = []
    auth_entity = indieauth.IndieAuth(id='http://foo.com/', user_json='{}')
    for username in 'a', 'b', 'c', 'd', 'e', 'f', 'g':
      source = Instagram.new(
        None, auth_entity=auth_entity, features=['listen'],
        actor={'username': username, 'image': {'url': 'http://old/pic'}})
      sources.append(source.put())

    resp = cron.application.get_response('/cron/update_instagram_pictures')
    self.assertEqual(200, resp.status_int)

    for i, source in enumerate(sources):
      self.assertEqual('http://new/pic' if i == 3 else 'http://old/pic',
                       source.get().picture)

  def test_update_instagram_picture_profile_404s(self):
    self.setup_instagram(batch_size=1)

    auth_entity = indieauth.IndieAuth(id='http://foo.com/', user_json='{}')
    source = Instagram.new(
        None, auth_entity=auth_entity, features=['listen'],
        actor={'username': 'x', 'image': {'url': 'http://old/pic'}})
    source.put()

    super(HandlerTest, self).expect_requests_get(
      gr_instagram.HTML_BASE_URL + 'x/', status_code=404, allow_redirects=False)
    self.mox.ReplayAll()

    resp = cron.application.get_response('/cron/update_instagram_pictures')
    self.assertEqual(200, resp.status_int)
    self.assertEquals('http://old/pic', source.key.get().picture)

  def test_update_flickr_pictures(self):
    self.expect_urlopen(
      'https://api.flickr.com/services/rest?'
        'user_id=39216764%40N00&nojsoncallback=1&'
        'method=flickr.people.getInfo&format=json',
      json_dumps({
        'person': {
          'id': '123@N00',
          'nsid': '123@N00',
          'iconfarm': 9,
          'iconserver': '9876',
        }}))
    self.mox.ReplayAll()

    self.flickr.put()
    resp = cron.application.get_response('/cron/update_flickr_pictures')
    self.assertEqual(200, resp.status_int)
    self.assertEquals(
      'https://farm9.staticflickr.com/9876/buddyicons/123@N00.jpg',
      self.flickr.key.get().picture)
