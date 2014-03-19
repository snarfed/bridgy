"""Unit tests for tasks.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import json
import mox

from activitystreams.oauth_dropins import twitter as oauth_twitter
import cron
import handlers
import testutil
from testutil import FakeSource, ModelsTest
from twitter import Twitter


class CronTest(ModelsTest):

  def setUp(self):
    super(ModelsTest, self).setUp()
    handlers.SOURCES['fake'] = FakeSource

  def tearDown(self):
    del handlers.SOURCES['fake']
    super(ModelsTest, self).tearDown()

  def test_replace_poll_tasks(self):
    self.assertEqual([], self.taskqueue_stub.GetTasks('poll'))
    now = datetime.datetime.now()

    # a bunch of sources, one needs a new poll task
    five_min_ago = now - datetime.timedelta(minutes=5)
    sources = [
      FakeSource.new(None).put(),  # not signed up for listen
      FakeSource.new(None, features=['listen'], last_polled=now).put(),
      FakeSource.new(None, features=['listen'], last_polled=five_min_ago).put(),
      FakeSource.new(None, features=['listen'], status='disabled').put(),
      FakeSource.new(None, features=['listen']).put(),  # needs a new poll task
      ]
    resp = cron.application.get_response('/cron/replace_poll_tasks')
    self.assertEqual(200, resp.status_int)

    tasks = self.taskqueue_stub.GetTasks('poll')
    self.assertEqual(1, len(tasks))
    params = testutil.get_task_params(tasks[0])
    self.assert_equals(sources[4].urlsafe(), params['source_key'])

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
    self.expect_urlopen(cron.TWITTER_API_USER_LOOKUP % 'a,c,b',
                        json.dumps(user_objs))
    self.mox.ReplayAll()

    resp = cron.application.get_response('/cron/update_twitter_pictures')
    self.assertEqual(200, resp.status_int)

    self.assertEquals('http://pi.ct/ure', sources[0].get().picture)
    self.assertEquals('http://new/pic.jpg', sources[1].get().picture)
