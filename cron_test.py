"""Unit tests for tasks.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import json

from activitystreams import oauth_dropins
from activitystreams.oauth_dropins import twitter as oauth_twitter
import cron
import handlers
import testutil
from testutil import FakeSource, ModelsTest
from twitter import Twitter


class CronTest(ModelsTest):

  def setUp(self):
    super(ModelsTest, self).setUp()
    oauth_dropins.appengine_config.TWITTER_APP_KEY = 'my_app_key'
    oauth_dropins.appengine_config.TWITTER_APP_SECRET = 'my_app_secret'
    handlers.SOURCES['fake'] = FakeSource

  def tearDown(self):
    del handlers.SOURCES['fake']
    super(ModelsTest, self).tearDown()

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
      FakeSource.new(None, last_polled=now, **defaults).put(),
      FakeSource.new(None, last_polled=five_min_ago, **defaults).put(),
      FakeSource.new(None, status='disabled', **defaults).put(),
      FakeSource.new(None, status='disabled', **defaults).put(),
      # need a new poll task
      FakeSource.new(None, status='enabled', **defaults).put(),
      FakeSource.new(None, status='error', **defaults).put(),
      # not signed up for listen
      FakeSource.new(None, last_webmention_sent=day_and_half_ago).put(),
      # never sent a webmention, past grace period. last polled is older than 2x
      # fast poll, but within 2x slow poll.
      FakeSource.new(None, features=['listen'], created=month_ago,
                     last_polled=day_and_half_ago).put(),
      ]
    resp = cron.application.get_response('/cron/replace_poll_tasks')
    self.assertEqual(200, resp.status_int)

    tasks = self.taskqueue_stub.GetTasks('poll')
    self.assertEqual(2, len(tasks))
    self.assert_equals(sources[4].urlsafe(),
                       testutil.get_task_params(tasks[0])['source_key'])
    self.assert_equals(sources[5].urlsafe(),
                       testutil.get_task_params(tasks[1])['source_key'])

  def test_update_twitter_pictures(self):
    sources = []
    for screen_name in ('a', 'b', 'c'):
      auth_entity = oauth_twitter.TwitterAuth(
        id='id', token_key='key', token_secret='secret',
        user_json=json.dumps({'name': 'Ryan',
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

    cron.USERS_PER_LOOKUP = 2
    self.expect_urlopen(cron.TWITTER_API_USER_LOOKUP % 'a,c',
                        json.dumps(user_objs))
    self.expect_urlopen(cron.TWITTER_API_USER_LOOKUP % 'b',
                        json.dumps(user_objs))
    self.mox.ReplayAll()

    resp = cron.application.get_response('/cron/update_twitter_pictures')
    self.assertEqual(200, resp.status_int)

    self.assertEquals('http://pi.ct/ure', sources[0].get().picture)
    self.assertEquals('http://new/pic.jpg', sources[1].get().picture)
