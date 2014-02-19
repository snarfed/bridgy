"""Unit tests for tasks.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import json
import logging
import mox
import urllib
import urllib2
import urlparse

from apiclient import errors
from google.appengine.api import memcache
from google.appengine.ext import ndb
import httplib2
from python_instagram.bind import InstagramAPIError
import requests

from appengine_config import HTTP_TIMEOUT
import models
import models_test
import tasks
from tasks import Poll, Propagate
import testutil
import util
from webmentiontools import send


NOW = datetime.datetime.now()
tasks.now_fn = lambda: NOW


class TaskQueueTest(testutil.ModelsTest):
  """Attributes:
    post_url: the URL for post_task() to post to
  """
  post_url = None

  def post_task(self, expected_status=200, params={}, **kwargs):
    """Args:
      expected_status: integer, the expected HTTP return code
    """
    resp = tasks.application.get_response(self.post_url, method='POST',
                                          body=urllib.urlencode(params),
                                          **kwargs)
    self.assertEqual(expected_status, resp.status_int)


class PollTest(TaskQueueTest):

  post_url = '/_ah/queue/poll'

  def post_task(self, expected_status=200):
    super(PollTest, self).post_task(
      expected_status=expected_status,
      params={'source_key': self.sources[0].key.urlsafe(),
              'last_polled': '1970-01-01-00-00-00'})

  def assert_responses(self):
    """Asserts that all of self.responses are saved."""
    # ndb's auto_now=True only happens when an entity is saved
    for resp in self.responses:
      resp.put()
    self.assert_entities_equal(self.responses, models.Response.query())

  def test_poll(self):
    """A normal poll task."""
    self.assertEqual([], list(models.Response.query()))
    self.assertEqual([], self.taskqueue_stub.GetTasks('poll'))

    self.post_task()
    self.assert_responses()

    source = self.sources[0].key.get()
    self.assertEqual(NOW, source.last_polled)

    tasks = self.taskqueue_stub.GetTasks('propagate')
    for task in tasks:
      self.assertEqual('/_ah/queue/propagate', task['url'])
    keys = set(ndb.Key(urlsafe=testutil.get_task_params(t)['response_key'])
               for t in tasks)
    self.assert_equals(keys, set(r.key for r in self.responses))

    tasks = self.taskqueue_stub.GetTasks('poll')
    self.assertEqual(1, len(tasks))
    self.assertEqual('/_ah/queue/poll', tasks[0]['url'])
    params = testutil.get_task_params(tasks[0])
    self.assert_equals(source.key.urlsafe(), params['source_key'])

  def test_poll_error(self):
    """If anything goes wrong, the source status should be set to 'error'."""
    self.mox.StubOutWithMock(testutil.FakeSource, 'get_activities_response')
    testutil.FakeSource.get_activities_response(
      count=mox.IgnoreArg(), fetch_replies=True, fetch_likes=True,
      fetch_shares=True, etag=None, min_id=None, cache=mox.IgnoreArg(),
      ).AndRaise(Exception('foo'))
    self.mox.ReplayAll()

    self.assertRaises(Exception, self.post_task)
    source = self.sources[0].key.get()
    self.assertEqual('error', source.status)

  def test_reset_status_to_enabled(self):
    """After a successful poll, the source status should be set to 'enabled'."""
    self.sources[0].status = 'error'
    self.sources[0].put()

    self.post_task()
    source = self.sources[0].key.get()
    self.assertEqual('enabled', source.status)

  def test_original_post_discovery(self):
    """Target URLs should be extracted from attachments, tags, and text."""
    obj = self.activities[0]['object']
    obj['tags'] = [{'objectType': 'article', 'url': 'http://tar.get/a'},
                   {'objectType': 'person', 'url': 'http://pe.rs/on'},
                   ]
    obj['attachments'] = [{'objectType': 'article', 'url': 'http://tar.get/b'}]
    obj['content'] = 'foo http://tar.get/c bar (not.at endd) baz (tar.get d)'
    self.sources[0].set_activities([self.activities[0]])

    self.post_task()
    expected = ['http://tar.get/%s' % i for i in 'a', 'b', 'c', 'd']
    self.assert_equals(expected, self.responses[0].key.get().unsent)

  def test_non_html_url(self):
    """Target URLs that aren't HTML should be ignored."""
    obj = self.activities[0]['object']
    obj['tags'] = []
    obj['content'] = 'http://not/html'
    self.sources[0].set_activities([self.activities[0]])

    self.mox.StubOutWithMock(requests, 'head', use_mock_anything=True)
    resp = requests.Response()
    resp.url = 'http://not/html'
    resp.headers['content-type'] = 'application/pdf'
    requests.head('http://not/html', allow_redirects=True, timeout=HTTP_TIMEOUT
                  ).AndReturn(resp)

    self.mox.ReplayAll()
    self.post_task()
    self.assert_equals([], self.responses[0].key.get().unsent)

  def test_resolved_url(self):
    """A URL that redirects should be resolved."""
    obj = self.activities[0]['object']
    obj['tags'] = []
    obj['content'] = 'http://will/redirect'
    self.sources[0].set_activities([self.activities[0]])

    self.mox.StubOutWithMock(requests, 'head', use_mock_anything=True)
    resp = requests.Response()
    resp.url = 'http://final/url'
    resp.headers['content-type'] = 'text/html'
    requests.head('http://will/redirect', allow_redirects=True, timeout=HTTP_TIMEOUT
                  ).AndReturn(resp)

    self.mox.ReplayAll()
    self.post_task()
    self.assert_equals(['http://final/url'], self.responses[0].key.get().unsent)

  def test_resolve_url_fails(self):
    """A URL that fails to resolve should still be handled ok."""
    obj = self.activities[0]['object']
    obj['tags'] = []
    obj['content'] = 'http://fails/resolve'
    self.sources[0].set_activities([self.activities[0]])

    self.mox.stubs.UnsetAll()
    self.mox.StubOutWithMock(requests, 'head', use_mock_anything=True)
    requests.head('http://fails/resolve', allow_redirects=True, timeout=HTTP_TIMEOUT
                  ).AndRaise(Exception('foo'))

    self.mox.ReplayAll()
    self.post_task()
    self.assert_equals(['http://fails/resolve'],
                       self.responses[0].key.get().unsent)

  def test_invalid_and_blacklisted_urls(self):
    """Target URLs with domains in the blacklist should be ignored.

    Same with invalid URLs that can't be parsed by urlparse.
    """
    obj = self.activities[0]['object']
    obj['tags'] = [{'objectType': 'article', 'url': 'http://tar.get/good'}]
    obj['attachments'] = [{'objectType': 'article', 'url': 'http://foo]'}]
    obj['content'] = 'foo http://facebook.com/bad bar baz (brid.gy bad)'
    self.sources[0].set_activities([self.activities[0]])

    self.post_task()
    self.assert_equals(['http://tar.get/good'],
                       self.responses[0].key.get().unsent)

  def test_non_public_posts(self):
    """Only posts with to: @public should be propagated."""
    del self.activities[0]['object']['to']
    self.activities[1]['object']['to'] = [{'objectType':'group', 'alias':'@private'}]
    self.activities[2]['object']['to'] = [{'objectType':'group', 'alias':'@public'}]

    self.post_task()
    ids = set()
    for task in self.taskqueue_stub.GetTasks('propagate'):
      resp_key = ndb.Key(urlsafe=testutil.get_task_params(task)['response_key'])
      ids.add(json.loads(resp_key.get().activity_json)['id'])
    self.assert_equals(ids, set([self.activities[0]['id'], self.activities[2]['id']]))

  def test_existing_responses(self):
    """Poll should be idempotent and not touch existing response entities.
    """
    self.responses[0].status = 'complete'
    self.responses[0].put()

    self.post_task()
    self.assert_responses()
    self.assertEqual('complete', self.responses[0].key.get().status)

  def test_wrong_last_polled(self):
    """If the source doesn't have our last polled value, we should quit.
    """
    self.sources[0].last_polled = datetime.datetime.utcfromtimestamp(3)
    self.sources[0].put()
    self.post_task()
    self.assertEqual([], list(models.Response.query()))

  def test_no_source(self):
    """If the source doesn't exist, do nothing and let the task die.
    """
    self.sources[0].key.delete()
    self.post_task()
    self.assertEqual([], self.taskqueue_stub.GetTasks('poll'))

  def test_disabled_source(self):
    """If the source is disabled, do nothing and let the task die.
    """
    self.sources[0].status = 'disabled'
    self.sources[0].put()
    self.post_task()
    self.assertEqual([], self.taskqueue_stub.GetTasks('poll'))

  def test_disable_source_on_deauthorized(self):
    """If the source raises DisableSource, disable it.
    """
    source = self.sources[0]
    self.mox.StubOutWithMock(testutil.FakeSource, 'get_activities_response')
    testutil.FakeSource.get_activities_response(
      count=mox.IgnoreArg(), fetch_replies=True, fetch_likes=True,
      fetch_shares=True, etag=None, min_id=None, cache=mox.IgnoreArg(),
      ).AndRaise(models.DisableSource)
    self.mox.ReplayAll()

    source.status = 'enabled'
    source.put()
    self.post_task()
    source = source.key.get()
    self.assertEqual('disabled', source.status)

  def test_site_specific_disable_sources(self):
    """HTTP 401 and 400 '' for Instagram should disable the source."""
    try:
      for err in (urllib2.HTTPError('url', 401, 'msg', {}, None),
                  InstagramAPIError('400', 'OAuthAccessTokenException', 'foo')):
        self.mox.UnsetStubs()
        self.setUp()

        self.mox.StubOutWithMock(testutil.FakeSource, 'get_activities_response')
        testutil.FakeSource.get_activities_response(
          count=mox.IgnoreArg(), fetch_replies=True, fetch_likes=True,
          fetch_shares=True, etag=None, min_id=None, cache=mox.IgnoreArg(),
          ).AndRaise(err)
        self.mox.ReplayAll()

        self.post_task()
        source = self.sources[0].key.get()
        self.assertEqual('disabled', source.status)

    finally:
      self.mox.UnsetStubs()

  def test_rate_limiting_errors(self):
    """Finish the task on rate limiting errors."""
    try:
      for err in (InstagramAPIError('503', 'Rate limited', '...'),
                  # use tasks.errors to work around import module aliasing.
                  # evidently i didn't fix them all. :/
                  tasks.errors.HttpError(httplib2.Response({'status': 429}), ''),
                  urllib2.HTTPError('url', 403, 'msg', {}, None)):
        self.mox.UnsetStubs()
        self.mox.StubOutWithMock(testutil.FakeSource, 'get_activities_response')
        testutil.FakeSource.get_activities_response(
          count=mox.IgnoreArg(), fetch_replies=True, fetch_likes=True,
          fetch_shares=True, etag=None, min_id=None, cache=mox.IgnoreArg(),
          ).AndRaise(err)
        self.mox.ReplayAll()

        self.post_task()
        source = self.sources[0].key.get()
        self.assertEqual('error', source.status)
        self.mox.VerifyAll()

        # should have inserted a new poll task
        polls = self.taskqueue_stub.GetTasks('poll')
        self.assertEqual(1, len(polls))
        self.assertEqual('/_ah/queue/poll', polls[0]['url'])
        polls = self.taskqueue_stub.FlushQueue('poll')

    finally:
      self.mox.UnsetStubs()

  def test_etag(self):
    """If we see an ETag, we should send it with the next get_activities()."""
    self.sources[0]._set('etag', '"my etag"')
    self.post_task()

    source = self.sources[0].key.get()
    self.assertEqual('"my etag"', source.last_activities_etag)
    source.last_polled = util.EPOCH
    source.put()

    self.mox.StubOutWithMock(testutil.FakeSource, 'get_activities_response')
    testutil.FakeSource.get_activities_response(
      count=mox.IgnoreArg(), fetch_replies=True, fetch_likes=True,
      fetch_shares=True, etag='"my etag"', min_id='c', cache=mox.IgnoreArg(),
      ).AndReturn({'items': [], 'etag': '"new etag"'})

    self.mox.ReplayAll()
    self.post_task()

    source = self.sources[0].key.get()
    self.assertEqual('"new etag"', source.last_activities_etag)

  def test_last_activity_id(self):
    """We should store the last activity id seen and then send it as min_id."""
    self.sources[0].set_activities(list(reversed(self.activities)))
    self.post_task()

    source = self.sources[0].key.get()
    self.assertEqual('c', source.last_activity_id)
    source.last_polled = util.EPOCH
    source.put()

    self.mox.StubOutWithMock(testutil.FakeSource, 'get_activities_response')
    testutil.FakeSource.get_activities_response(
      count=mox.IgnoreArg(), fetch_replies=True, fetch_likes=True,
      fetch_shares=True, etag=None, min_id='c', cache=mox.IgnoreArg(),
      ).AndReturn({'items': []})

    self.mox.ReplayAll()
    self.post_task()


class PropagateTest(TaskQueueTest):

  post_url = '/_ah/queue/propagate'

  def setUp(self):
    super(PropagateTest, self).setUp()
    for r in self.responses[:3]:
      r.get_or_save()
    self.mox.StubOutClassWithMocks(send, 'WebmentionSend')

  def tearDown(self):
    self.mox.UnsetStubs()
    super(PropagateTest, self).tearDown()

  def post_task(self, expected_status=200, response=None, **kwargs):
    if response is None:
      response = self.responses[0]
    super(PropagateTest, self).post_task(
      expected_status=expected_status,
      params={'response_key': response.key.urlsafe()},
      **kwargs)

  def assert_response_is(self, status, leased_until=False, sent=[], error=[],
                         unsent=[], skipped=[], failed=[], response=None):
    """Asserts that responses[0] has the given values in the datastore.
    """
    if response is None:
      response = self.responses[0]
    response = response.key.get()
    self.assertEqual(status, response.status)
    if leased_until is not False:
      self.assertEqual(leased_until, response.leased_until)
    self.assert_equals(unsent, response.unsent)
    self.assert_equals(sent, response.sent)
    self.assert_equals(error, response.error)
    self.assert_equals(skipped, response.skipped)
    self.assert_equals(failed, response.failed)

  def expect_webmention(self, source_url=None, target='http://target1/post/url',
                        error=None, endpoint=None):
    if source_url is None:
      source_url = 'http://localhost/comment/fake/%s/a/1_2_a' % \
          self.sources[0].key.string_id()
    mock_send = send.WebmentionSend(source_url, target, endpoint=endpoint)
    mock_send.receiver_endpoint = (endpoint if endpoint
                                   else 'http://webmention/endpoint')
    mock_send.response = 'used in logging'
    mock_send.error = error
    return mock_send.send(timeout=999)

  def test_propagate(self):
    """Normal propagate tasks."""
    self.assertEqual('new', self.responses[0].status)

    id = self.sources[0].key.string_id()
    for url in ('http://localhost/comment/fake/%s/a/1_2_a' % id,
                'http://localhost/like/fake/%s/a/alice' % id,
                'http://localhost/repost/fake/%s/a/bob' % id):
      self.expect_webmention(source_url=url).AndReturn(True)
    self.mox.ReplayAll()

    for r in self.responses[:3]:
      self.post_task(response=r)
      self.assert_response_is('complete', NOW + Propagate.LEASE_LENGTH,
                              sent=['http://target1/post/url'], response=r)
      memcache.flush_all()

  def test_propagate_from_error(self):
    """A normal propagate task, with a response starting as 'error'."""
    self.responses[0].status = 'error'
    self.responses[0].put()

    self.expect_webmention().AndReturn(True)
    self.mox.ReplayAll()
    self.post_task()
    self.assert_response_is('complete', NOW + Propagate.LEASE_LENGTH,
                           sent=['http://target1/post/url'])

  def test_success_and_errors(self):
    """We should send webmentions to the unsent and error targets."""
    self.responses[0].unsent = ['http://1', 'http://2']
    self.responses[0].error = ['http://3', 'http://4', 'http://5']
    self.responses[0].sent = ['http://6']
    self.responses[0].put()

    self.expect_webmention(target='http://1').InAnyOrder().AndReturn(True)
    self.expect_webmention(target='http://2', error={'code': 'NO_ENDPOINT'})\
        .InAnyOrder().AndReturn(False)
    self.expect_webmention(target='http://3', error={'code': 'RECEIVER_ERROR'})\
        .InAnyOrder().AndReturn(False)
    self.expect_webmention(target='http://4',  # 4XX should go into 'failed'
                           error={'code': 'BAD_TARGET_URL', 'http_status': 404})\
        .InAnyOrder().AndReturn(False)
    self.expect_webmention(target='http://5',  # 5XX should go into 'error'
                           error={'code': 'BAD_TARGET_URL', 'http_status': 500})\
        .InAnyOrder().AndReturn(False)

    self.mox.ReplayAll()
    self.post_task(expected_status=Propagate.ERROR_HTTP_RETURN_CODE)
    response = self.responses[0].key.get()
    self.assert_response_is('error',
                            sent=['http://6', 'http://1'],
                            error=['http://3', 'http://5'],
                            failed=['http://4'],
                            skipped=['http://2'])

  def test_cached_webmention_discovery(self):
    """Webmention endpoints should be cached."""
    self.expect_webmention().AndReturn(True)
    # second webmention should use the cached endpoint
    self.expect_webmention(endpoint='http://webmention/endpoint').AndReturn(True)

    self.mox.ReplayAll()
    self.post_task()

    self.responses[0].status = 'new'
    self.responses[0].put()
    self.post_task()

  def test_cached_webmention_discovery_error(self):
    """Failed webmention discovery should be cached too."""
    self.expect_webmention(error={'code': 'NO_ENDPOINT'}).AndReturn(False)
    # second time shouldn't try to send a webmention

    self.mox.ReplayAll()
    self.post_task()
    self.assert_response_is('complete', skipped=['http://target1/post/url'])

    self.responses[0].status = 'new'
    self.responses[0].put()
    self.post_task()
    self.assert_response_is('complete', skipped=['http://target1/post/url'])

  def test_webmention_blacklist(self):
    """Target URLs with domains in the blacklist should be ignored.

    TODO: also invalid URLs that can't be parsed by urlparse?
    """
    self.responses[0].unsent = ['http://t.co/bad', 'http://foo/good']
    self.responses[0].error = ['http://instagr.am/bad',
                               # urlparse raises ValueError: Invalid IPv6 URL
                               'http://foo]']
    self.responses[0].put()

    self.expect_webmention(target='http://foo/good').AndReturn(True)
    self.mox.ReplayAll()

    self.post_task()
    self.assert_response_is('complete', sent=['http://foo/good'])

  def test_non_html_url(self):
    """Target URLs that aren't HTML should be ignored."""
    self.responses[0].unsent = ['http://not/html']
    self.responses[0].put()

    self.mox.StubOutWithMock(requests, 'head', use_mock_anything=True)
    resp = requests.Response()
    resp.url = 'http://not/html'
    resp.headers['content-type'] = 'application/mpeg'
    requests.head('http://not/html', allow_redirects=True, timeout=HTTP_TIMEOUT
                  ).AndReturn(resp)

    self.mox.ReplayAll()
    self.post_task()
    self.assert_response_is('complete')

  def test_no_targets(self):
    """No target URLs."""
    self.responses[0].unsent = []
    self.responses[0].put()

    self.mox.ReplayAll()
    self.post_task()
    self.assert_response_is('complete', NOW + Propagate.LEASE_LENGTH)

  def test_already_complete(self):
    """If the response has already been propagated, do nothing."""
    self.responses[0].status = 'complete'
    self.responses[0].put()

    self.post_task()
    self.assert_response_is('complete', unsent=['http://target1/post/url'])

  def test_leased(self):
    """If the response is processing and the lease hasn't expired, do nothing."""
    self.responses[0].status = 'processing'
    leased_until = NOW + datetime.timedelta(minutes=1)
    self.responses[0].leased_until = leased_until
    self.responses[0].put()

    self.post_task(expected_status=Propagate.ERROR_HTTP_RETURN_CODE)
    self.assert_response_is('processing', leased_until,
                            unsent=['http://target1/post/url'])

    response = self.responses[0].key.get()
    self.assertEqual('processing', response.status)
    self.assertEqual(leased_until, response.leased_until)

  def test_lease_expired(self):
    """If the response is processing but the lease has expired, process it."""
    self.responses[0].status = 'processing'
    self.responses[0].leased_until = NOW - datetime.timedelta(minutes=1)
    self.responses[0].put()

    self.expect_webmention().AndReturn(True)
    self.mox.ReplayAll()
    self.post_task()
    self.assert_response_is('complete', NOW + Propagate.LEASE_LENGTH,
                           sent=['http://target1/post/url'])

  def test_no_response(self):
    """If the response doesn't exist, the request should fail."""
    self.responses[0].key.delete()
    self.post_task(expected_status=Propagate.ERROR_HTTP_RETURN_CODE)

  def test_no_source(self):
    """If the source doesn't exist, the request should give up."""
    self.sources[0].key.delete()
    self.post_task(expected_status=200)

  def test_non_public_activity(self):
    """If the activity is non-public, we should give up."""
    activity = json.loads(self.responses[0].activity_json)
    activity['to'] = [{'objectType':'group', 'alias':'@private'}]
    self.responses[0].activity_json = json.dumps(activity)
    self.responses[0].put()

    self.post_task()
    self.assert_response_is('complete', unsent=['http://target1/post/url'], sent=[])

  def test_non_public_response(self):
    """If the response is non-public, we should give up."""
    resp = json.loads(self.responses[0].response_json)
    resp['to'] = [{'objectType':'group', 'alias':'@private'}]
    self.responses[0].response_json = json.dumps(resp)
    self.responses[0].put()

    self.post_task()
    self.assert_response_is('complete', unsent=['http://target1/post/url'], sent=[])

  def test_webmention_fail(self):
    """If sending the webmention fails, the lease should be released."""
    for code, give_up in (('NO_ENDPOINT', True),
                          ('BAD_TARGET_URL', False),
                          ('RECEIVER_ERROR', False)):
      self.mox.UnsetStubs()
      self.setUp()
      self.responses[0].status = 'new'
      self.responses[0].put()
      self.expect_webmention(error={'code': code, 'http_status': 500})\
          .AndReturn(False)
      self.mox.ReplayAll()

      logging.debug('Testing %s', code)
      expected_status = 200 if give_up else Propagate.ERROR_HTTP_RETURN_CODE
      self.post_task(expected_status=expected_status)
      if give_up:
        self.assert_response_is('complete', skipped=['http://target1/post/url'])
      else:
        self.assert_response_is('error', error=['http://target1/post/url'])
      self.mox.VerifyAll()

  def test_webmention_fail_and_succeed(self):
    """All webmentions should be attempted, but any failure sets error status."""
    self.responses[0].unsent = ['http://first', 'http://second']
    self.responses[0].put()
    self.expect_webmention(target='http://first', error={'code': 'FOO'})\
        .AndReturn(False)
    self.expect_webmention(target='http://second').AndReturn(True)

    self.mox.ReplayAll()
    self.post_task(expected_status=Propagate.ERROR_HTTP_RETURN_CODE)
    self.assert_response_is('error', None, error=['http://first'],
                           sent=['http://second'])

  def test_webmention_exception(self):
    """Exceptions on individual target URLs shouldn't stop the whole task."""
    self.responses[0].unsent = ['http://error', 'http://good']
    self.responses[0].put()
    self.expect_webmention(target='http://error').AndRaise(Exception('foo'))
    self.expect_webmention(target='http://good').AndReturn(True)
    self.mox.ReplayAll()

    self.post_task(expected_status=Propagate.ERROR_HTTP_RETURN_CODE)
    self.assert_response_is('error', None, error=['http://error'],
                            sent=['http://good'])

  def test_translate_appspot_to_bridgy(self):
    """Tasks on brid.gy should use brid-gy.appspot.com as the source URL."""
    self.responses[0].unsent = ['http://good']
    self.responses[0].put()
    source_url = 'https://brid-gy.appspot.com/comment/fake/%s/a/1_2_a' % \
        self.sources[0].key.string_id()
    self.expect_webmention(source_url=source_url, target='http://good')\
        .AndReturn(True)

    self.mox.ReplayAll()
    self.post_task(base_url='http://www.brid.gy')

  def test_translate_http_to_https(self):
    """Tasks on brid-gy.appspot.com should always use https in the source URL.

    TODO: unify with test_translate_appspot_to_bridgy()
    """
    self.responses[0].unsent = ['http://good']
    self.responses[0].put()
    source_url = 'https://brid-gy.appspot.com/comment/fake/%s/a/1_2_a' % \
        self.sources[0].key.string_id()
    self.expect_webmention(source_url=source_url, target='http://good')\
        .AndReturn(True)

    self.mox.ReplayAll()
    self.post_task(base_url='http://brid-gy.appspot.com')

  def test_complete_exception(self):
    """If completing raises an exception, the lease should be released."""
    self.expect_webmention().AndReturn(True)
    self.mox.StubOutWithMock(Propagate, 'complete_response')
    Propagate.complete_response(mox.IgnoreArg()).AndRaise(Exception('foo'))
    self.mox.ReplayAll()

    self.post_task(expected_status=500)
    self.assert_response_is('error', None, sent=['http://target1/post/url'])
