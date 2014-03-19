"""Unit tests for tasks.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import mox

import cron
import handlers
import testutil
from testutil import FakeSource, ModelsTest


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
