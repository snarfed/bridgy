#!/usr/bin/python
"""Unit tests for facebook.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import logging
import mox
import json
import testutil
import urllib

import facebook
from facebook import FacebookApp, FacebookComment, FacebookPage
import models
import tasks_test

from google.appengine.api import urlfetch
from google.appengine.ext import webapp


class FacebookTestBase(tasks_test.TaskQueueTest):

  def setUp(self):
    super(FacebookTestBase, self).setUp()

    FacebookApp(app_id='app_id', app_secret='app_secret').save()
    self.app = FacebookApp.get()

  def expect_fql(self, query_snippet, results):
    """Stubs out and expects an FQL query via urlfetch.

    Expects my_access_token to be used as the access token.

    Args:
      query_snippet: an unescaped snippet that should be in the query
      results: list or dict of results to return
    """
    url_re = ('^https://api.facebook.com/method/fql.query\?'
              '&access_token=my_access_token&format=json&query=(.*)$')
    comparator = mox.Regex(url_re)
      
    if query_snippet:
      quoted = urllib.quote(query_snippet)
      comparator = mox.And(comparator, mox.StrContains(quoted))

    self.expect_urlfetch(comparator, json.dumps(results))


def FacebookAppTest(FacebookTestBase):

  def test_get_access_token(self):
    self.app.get_access_token(self.handler, '/redirect_to')
    self.assertEqual(302, self.handler.response.status)
    self.assertEqual(
      'http://www.facebook.com/dialog/oauth/?&scope=read_stream,offline_access&client_id=app_id&redirect_uri=http://HOST/facebook/got_auth_code&response_type=code&state=http://HOST/redirect_to',
      self.handler.response.headers['Location'])

  def test_got_auth_code(self):
    self.expect_urlfetch(
      'https://graph.facebook.com/oauth/access_token?&client_id=app_id&redirect_uri=http://HOST/facebook/got_access_token&client_secret=app_secret&code=my_auth_code',
      'foo=bar&access_token=my_access_token')

    self.mox.ReplayAll()
    url = '/facebook/got_auth_code?code=my_auth_code&state=http://my/redirect_to'
    resp = self.get(facebook.application, url, 302)
    self.assertEqual('http://my/redirect_to?access_token=my_access_token',
                     resp.headers['Location'])

  def test_run(self):
    self.assertEqual(0, FacebookPage.all().count())
    resp = self.post(facebook.application, '/facebook/go', 302)
    self.assertEqual(1, FacebookPage.all().count())
    # TODO:  make this work
    # resp = self.post(application, '/facebook/go', 200)
    # self.assertEqual(1, FacebookPage.all().count())

  def test_fql(self):
    self.expect_fql('my_query', {'my_key': [ 'my_list']})
    self.mox.ReplayAll()
    self.assertEqual({'my_key': ['my_list']},
                     self.app.fql('my_query', 'my_access_token'))


class FacebookPageTest(FacebookTestBase):

  def setUp(self):
    super(FacebookPageTest, self).setUp()
    self.user = models.User.get_or_insert_current_user(self.handler)
    self.handler.messages = []
    self.page = FacebookPage(key_name='2468',
                             name='my full name',
                             url='http://my.fb/url',
                             pic_small='http://my.pic/small',
                             type='user',
                             username='my_username',
                             access_token='my_access_token',
                             )
    self.new_fql_results = [{
        'id': '2468',
        'name': 'my full name',
        'url': 'http://my.fb/url',
        'pic_small': 'http://my.pic/small',
        'type': 'user',
        'username': 'my_username',
        }]

    task_name = str(self.page.key()) + '_1970-01-01-00-00-00'
    self.setup_taskqueue(task_name, '/_ah/queue/poll')

  def _test_new(self):
    self.expect_fql('FROM profile WHERE id = me()', self.new_fql_results)
    self.mox.ReplayAll()

    got = FacebookPage.new('my_access_token', self.handler)
    self.assert_entities_equal(self.page, got, ignore=['created'])
    self.assert_entities_equal([self.page], FacebookPage.all(), ignore=['created'])
    self.assertEqual([self.page.key()], models.User.get_current_user().sources)

    tasks = self.taskqueue_stub.GetTasks('poll')
    self.assertEqual(1, len(tasks))
    self.assertEqual(self.task_name, tasks[0]['name'])
    self.assertEqual('/_ah/queue/poll', tasks[0]['url'])

  def test_new(self):
    self._test_new()
    self.assertEqual(self.handler.messages, ['Added Facebook page: my full name'])

  def test_new_already_exists(self):
    self.page.save()
    self._test_new()
    self.assertEqual(self.handler.messages,
                     ['Updated existing Facebook page: my full name'])

  def test_new_user_already_owns(self):
    self.user.sources = [self.page.key()]
    self.user.save()
    self._test_new()

  def test_poll(self):
    # note that json requires double quotes. :/
    results = [
       {'post_fbid': '123', 'object_id': '001', 'fromid': 456,
        'username': '', 'time': 1, 'text': 'foo'},
       {'post_fbid': '789', 'object_id': '002', 'fromid': 0,
        'username': 'my_username', 'time': 2, 'text': 'bar'},
      ]
    self.expect_fql('WHERE owner = 2468', results)

    self.mox.ReplayAll()
    expected = [
      FacebookComment(key_name='123', source=self.page, object_id='001',
                      fromid=456, username='',
                      created=datetime.datetime.utcfromtimestamp(1),
                      content='foo'),
      FacebookComment(key_name='789', source=self.page, object_id='002',
                      fromid=0, username='my_username',
                      created=datetime.datetime.utcfromtimestamp(2),
                      content='bar'),
      ]
    got = self.page.poll()
    self.assert_entities_equal(expected, got)

  # def test_initialize_fresh(self):
  #   self.mox.StubOutWithMock(self.app, 'get_access_token')
  #   self.app.get_access_token(self.handler, '/sources/facebook/got_access_token')

  #   self.mox.ReplayAll()
  #   FacebookPage(key_name='foo').initialize()

