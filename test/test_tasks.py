# coding=utf-8
"""Unit tests for tasks.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import bz2
import copy
import datetime
import json
import logging
import mox
import StringIO
import time
import urllib
import urllib2

import apiclient
from google.appengine.api import memcache
from google.appengine.api.datastore_types import _MAX_STRING_LENGTH
from google.appengine.ext import ndb
import httplib2
from oauth2client.client import AccessTokenRefreshError
import requests
from webmentiontools import send

import models
import tasks
from tasks import PropagateResponse
import testutil
from testutil import FakeSource, FakeGrSource
import util

NOW = datetime.datetime.utcnow()
tasks.now_fn = lambda: NOW

ERROR_HTTP_RETURN_CODE = tasks.SendWebmentions.ERROR_HTTP_RETURN_CODE
LEASE_LENGTH = tasks.SendWebmentions.LEASE_LENGTH


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

  def setUp(self):
    super(PollTest, self).setUp()
    FakeGrSource.DOMAIN = 'source'

  def tearDown(self):
    FakeGrSource.DOMAIN = 'fa.ke'
    super(PollTest, self).tearDown()

  def post_task(self, expected_status=200, source=None, reset=False):
    if source is None:
      source = self.sources[0]

    if reset:
      source = source.key.get()
      source.last_polled = util.EPOCH
      source.put()

    super(PollTest, self).post_task(
      expected_status=expected_status,
      params={'source_key': source.key.urlsafe(),
              'last_polled': '1970-01-01-00-00-00'})

  def assert_responses(self):
    """Asserts that all of self.responses are saved."""
    # sort fields in json properties since they're compared as strings
    stored = list(models.Response.query())
    for resp in self.responses + stored:
      resp.activities_json = [json.dumps(json.loads(a), sort_keys=True)
                              for a in resp.activities_json]
      resp.response_json = json.dumps(json.loads(resp.response_json), sort_keys=True)
    self.assert_entities_equal(self.responses, stored, ignore=('created', 'updated'))

  def assert_task_eta(self, countdown):
    """Checks the current poll task's eta. Handles the random range.

    Args:
      countdown: datetime.timedelta
    """
    task = self.taskqueue_stub.GetTasks('poll')[0]
    # 10s padding
    delta = datetime.timedelta(seconds=(countdown.total_seconds() * .2) + 10)
    # use actual current time, not NOW, because the app engine SDK does
    self.assertAlmostEqual(datetime.datetime.utcnow() + countdown,
                           testutil.get_task_eta(task), delta=delta)

  def test_poll(self):
    """A normal poll task."""
    self.assertEqual(0, models.Response.query().count())
    self.assertEqual([], self.taskqueue_stub.GetTasks('poll'))

    self.post_task()
    self.assertEqual(9, models.Response.query().count())
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
    self.assert_task_eta(FakeSource.FAST_POLL)
    params = testutil.get_task_params(tasks[0])
    self.assert_equals(source.key.urlsafe(), params['source_key'])

  def test_poll_error(self):
    """If anything goes wrong, the source status should be set to 'error'."""
    self.mox.StubOutWithMock(FakeSource, 'get_activities_response')
    FakeSource.get_activities_response(
      count=mox.IgnoreArg(), fetch_replies=True, fetch_likes=True,
      fetch_shares=True, etag=None, min_id=None, cache=mox.IgnoreArg(),
      ).AndRaise(Exception('foo'))
    self.mox.ReplayAll()

    self.assertRaises(Exception, self.post_task)
    source = self.sources[0].key.get()
    self.assertEqual('error', source.status)
    self.assertEqual(0, len(self.taskqueue_stub.GetTasks('poll')))

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
    obj.update({
      'upstreamDuplicates': ['http://tar.get/a'],
      'tags': [
        {'objectType': 'article', 'url': 'http://tar.get/b'},
        {'objectType': 'mention', 'url': 'http://tar.get/c'},
        {'objectType': 'person', 'url': 'http://pe.rs/on'},
      ],
      'attachments': [{'objectType': 'article', 'url': 'http://tar.get/d'}],
      'content': 'foo http://tar.get/e bar (not.at endd) baz (tar.get f)',
    })
    self.sources[0].set_activities([self.activities[0]])

    self.post_task()
    expected = ['http://tar.get/%s' % i for i in 'a', 'b', 'c', 'd', 'e', 'f']
    self.assert_equals(expected, self.responses[0].key.get().unsent)

  def test_original_post_discovery_dedupes(self):
    """Target URLs should be deduped, ignoring scheme (http vs https)."""
    obj = self.activities[0]['object']
    obj['tags'] = [{'objectType': 'article', 'url': 'https://tar.get/a'}]
    obj['attachments'] = [{'objectType': 'article', 'url': 'http://tar.get/a'}]
    obj['content'] = 'foo https://tar.get/a bar (tar.get a)'
    self.sources[0].set_activities([self.activities[0]])

    self.post_task()
    self.assert_equals(['https://tar.get/a'], self.responses[0].key.get().unsent)

  def test_non_html_url(self):
    """Target URLs that aren't HTML should be ignored."""
    obj = self.activities[0]['object']
    obj['tags'] = []
    obj['content'] = 'http://not/html'
    self.sources[0].set_activities([self.activities[0]])

    self.expect_requests_head('http://not/html', content_type='application/pdf')

    self.mox.ReplayAll()
    self.post_task()
    self.assert_equals([], self.responses[0].key.get().unsent)

  def test_resolved_url(self):
    """A URL that redirects should be resolved."""
    obj = self.activities[0]['object']
    obj['tags'] = []
    obj['content'] = 'http://will/redirect'
    self.sources[0].set_activities([self.activities[0]])

    self.expect_requests_head('http://will/redirect', redirected_url='http://final/url')

    self.mox.ReplayAll()
    self.post_task()
    self.assert_equals(['http://final/url'], self.responses[0].key.get().unsent)

  def test_resolve_url_fails(self):
    """A URL that fails to resolve should still be handled ok."""
    obj = self.activities[0]['object']
    obj['tags'] = []
    obj['content'] = 'http://fails/resolve'
    self.sources[0].set_activities([self.activities[0]])

    self.expect_requests_head('http://fails/resolve', status_code=400)

    self.mox.ReplayAll()
    self.post_task()
    self.assert_equals(['http://fails/resolve'],
                       self.responses[0].key.get().unsent)

  def test_non_html_file_extension(self):
    """If our HEAD request fails, we should infer type from file extension."""
    self.activities[0]['object'].update({'tags': [], 'content': 'http://a.zip'})
    self.sources[0].set_activities([self.activities[0]])

    self.expect_requests_head('http://a.zip', status_code=405,
                              # we should ignore an error response's content type
                              content_type='text/html')

    self.mox.ReplayAll()
    self.post_task()
    self.assert_equals([], self.responses[0].key.get().unsent)

  def test_invalid_and_blacklisted_urls(self):
    """Target URLs with domains in the blacklist should be ignored.

    Same with invalid URLs that can't be parsed by urlparse.
    """
    obj = self.activities[0]['object']
    obj['tags'] = [{'objectType': 'article', 'url': 'http://tar.get/good'}]
    obj['attachments'] = [{'objectType': 'article', 'url': 'http://foo]'}]
    obj['content'] = 'foo http://facebook.com/bad bar baz (t.co bad)'
    self.sources[0].set_activities([self.activities[0]])

    self.post_task()
    self.assert_equals(['http://tar.get/good'],
                       self.responses[0].key.get().unsent)

  def test_strip_utm_query_params(self):
    """utm_* query params should be stripped from target URLs."""
    obj = self.activities[0]['object']
    obj.update({'content': '',
                'attachments': [],
                'tags': [{'objectType': 'article',
                          'url': 'http://foo/bar?a=b&utm_medium=x&utm_source=y'}],
                })
    self.sources[0].set_activities([self.activities[0]])

    self.post_task()
    self.assert_equals(['http://foo/bar?a=b'],
                       self.responses[0].key.get().unsent)

  def test_strip_utm_query_params_after_redirect(self):
    """utm_* query params should also be stripped after redirects."""
    for a in self.activities:
      a['object']['tags'][0]['id'] = 'tag:source.com,2013:only_reply'
      del a['object']['tags'][1], a['object']['replies']

    # test with two activities so we can check urls_to_activity.
    # https://github.com/snarfed/bridgy/issues/237
    self.activities[0]['object'].update({'content': 'http://redir/0'})
    self.activities[1]['object'].update({'content': 'http://redir/1'})
    self.sources[0].set_activities(self.activities[0:2])

    self.expect_requests_head('http://redir/0',
                              redirected_url='http://first?utm_medium=x')
    self.expect_requests_head('http://redir/1',
                              redirected_url='http://second?utm_source=Twitter')
    self.mox.ReplayAll()
    self.post_task()

    self.assertEquals(1, models.Response.query().count())
    resp = models.Response.query().get()
    self.assert_equals(['http://first', 'http://second'], resp.unsent)
    self.assert_equals({'http://first': 0, 'http://second': 1},
                       json.loads(resp.urls_to_activity))

  def test_too_long_urls(self):
    """URLs longer than the datastore's limit should be truncated and skipped.

    https://github.com/snarfed/bridgy/issues/273
    """
    self.activities[0]['object'].update({'tags': [], 'content': 'http://first'})
    self.sources[0].set_activities([self.activities[0]])

    too_long = 'http://host/' + 'x' * _MAX_STRING_LENGTH
    self.expect_requests_head('http://first', redirected_url=too_long)

    self.mox.ReplayAll()
    self.post_task()
    resp = self.responses[0].key.get()
    self.assert_equals([], resp.unsent)
    self.assert_equals([too_long[:_MAX_STRING_LENGTH - 4] + '...'], resp.failed)

  def test_non_public_posts(self):
    """Only posts with to: @public should be propagated."""
    del self.activities[0]['object']['to']
    self.activities[1]['object']['to'] = [{'objectType':'group', 'alias':'@private'}]
    self.activities[2]['object']['to'] = [{'objectType':'group', 'alias':'@public'}]

    self.post_task()
    ids = set()
    for task in self.taskqueue_stub.GetTasks('propagate'):
      resp_key = ndb.Key(urlsafe=testutil.get_task_params(task)['response_key'])
      ids.update(json.loads(a)['id'] for a in resp_key.get().activities_json)
    self.assert_equals(ids, set([self.activities[0]['id'], self.activities[2]['id']]))

  def test_no_responses(self):
    """Handle activities without responses ok.
    """
    activities = self.sources[0].get_activities()
    for a in activities:
      a['object'].update({'replies': {}, 'tags': []})
    self.sources[0].set_activities(activities)

    self.post_task()
    self.assert_equals([], list(models.Response.query()))

  def test_existing_responses(self):
    """Poll should be idempotent and not touch existing response entities.
    """
    self.responses[0].status = 'complete'
    self.responses[0].put()

    self.post_task()
    self.assert_responses()
    self.assertEqual('complete', self.responses[0].key.get().status)

  def test_existing_response_with_fb_id(self):
    """We should de-dupe responses using fb_id as well as id.

    https://github.com/snarfed/bridgy/issues/305#issuecomment-94004416
    """
    self.activities[0]['object']['replies']['items'][0]['fb_id'] = '12:34:56_78'
    fb_id_resp = models.Response(id='tag:facebook.com,2013:12:34:56_78',
                                 **self.responses[0].to_dict())
    fb_id_resp.status = 'complete'
    fb_id_resp.put()

    self.post_task()
    self.assertIsNone(self.responses[0].key.get())
    self.assertEqual('complete', fb_id_resp.key.get().status)


  def test_same_response_for_multiple_activities(self):
    """Should combine the original post URLs from all of them.
    """
    for a in self.activities:
      a['object']['replies']['items'][0]['id'] = 'tag:source.com,2013:only_reply'
      a['object']['tags'] = []
      del a['object']['url']  # prevent posse post discovery (except 2, below)

    self.activities[1]['object'].update({
        'content': '',
        'attachments': [{'objectType': 'article', 'url': 'http://from/tag'}]})
    self.activities[2]['object'].update({
        'content': '',
        'url': 'https://activ/2'})

    self.sources[0].set_activities(self.activities)

    # trigger posse post discovery
    self.sources[0].domain_urls = ['http://author']
    self.sources[0].put()
    models.SyndicatedPost(parent=self.sources[0].key,
                          original='http://from/synd/post',
                          syndication='https://activ/2').put()

    self.post_task()
    self.assertEquals(1, len(self.taskqueue_stub.GetTasks('propagate')))
    self.assertEquals(1, models.Response.query().count())
    resp = models.Response.query().get()
    self.assert_equals(['tag:source.com,2013:%s' % id for id in 'a', 'b', 'c'],
                       [json.loads(a)['id'] for a in resp.activities_json])
    self.assert_equals(
      ['http://from/tag', 'http://from/synd/post', 'http://target1/post/url'],
      resp.unsent)
    self.assert_equals({'http://from/tag': 1,
                        'http://from/synd/post': 2,
                        'http://target1/post/url': 0},
                       json.loads(resp.urls_to_activity))

  def test_multiple_activities_no_target_urls(self):
    """Response.urls_to_activity should be left unset.
    """
    for a in self.activities:
      a['object']['replies']['items'][0]['id'] = 'tag:source.com,2013:only_reply'
      a['object']['tags'] = []
      del a['object']['url']  # prevent posse post discovery
      del a['object']['content']
    self.sources[0].set_activities(self.activities)

    self.post_task()
    resp = models.Response.query().get()
    self.assert_equals([], resp.unsent)
    self.assert_equals('complete', resp.status)
    self.assertIsNone(resp.urls_to_activity)

  def test_mentions_only_go_to_posts_and_comments(self):
    """Response.urls_to_activity should be left unset.
    """
    self.sources[0].domains = ['foo']
    self.sources[0].put()

    del self.activities[0]['object']['url']  # prevent posse post discovery
    self.sources[0].set_activities([self.activities[0]])

    self.post_task()

    self.assert_equals('comment', self.responses[0].type)
    self.responses[0].unsent = ['http://target1/post/url']
    for resp in self.responses[1:3]:
      self.assertNotIn(resp.type, ('post', 'comment'))
      resp.unsent = []
      resp.status = 'complete'

    self.assert_entities_equal(
      self.responses[:3], models.Response.query().fetch(),
      ignore=('created', 'updated', 'activities_json', 'response_json'))

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

  def test_source_without_listen_feature(self):
    """If the source doesn't have the listen feature, let the task die.
    """
    self.sources[0].features = []
    self.sources[0].put()
    self.post_task()
    self.assertEqual([], self.taskqueue_stub.GetTasks('poll'))

  def test_disable_source_on_deauthorized(self):
    """If the source raises DisableSource, disable it.
    """
    source = self.sources[0]
    self.mox.StubOutWithMock(FakeSource, 'get_activities_response')
    FakeSource.get_activities_response(
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
      for err in (
          urllib2.HTTPError('url', 401, 'msg', {}, StringIO.StringIO('body')),
          urllib2.HTTPError('url', 400, 'foo', {}, StringIO.StringIO(
            '{"meta":{"error_type":"OAuthAccessTokenException"}}')),
          AccessTokenRefreshError('invalid_grant'),
          AccessTokenRefreshError('invalid_grant: Token has been revoked.'),
      ):
        self.mox.UnsetStubs()
        self.setUp()

        self.mox.StubOutWithMock(FakeSource, 'get_activities_response')
        FakeSource.get_activities_response(
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
      error_body = json.dumps({"meta": {
        "code": 429, "error_message": "The maximum number of requests...",
        "error_type": "OAuthRateLimitException"}})
      for err in (
          urllib2.HTTPError('url', 429, 'Rate limited', {},
                            StringIO.StringIO(error_body)),
          apiclient.errors.HttpError(httplib2.Response({'status': 429}), ''),
          urllib2.HTTPError('url', 403, 'msg', {}, None)
      ):
        self.mox.UnsetStubs()
        self.mox.StubOutWithMock(FakeSource, 'get_activities_response')
        FakeSource.get_activities_response(
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
        self.assert_task_eta(FakeSource.RATE_LIMITED_POLL)
        self.taskqueue_stub.FlushQueue('poll')

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

    self.mox.StubOutWithMock(FakeSource, 'get_activities_response')
    FakeSource.get_activities_response(
      count=mox.IgnoreArg(), fetch_replies=True, fetch_likes=True,
      fetch_shares=True, etag='"my etag"', min_id='c', cache=mox.IgnoreArg(),
      ).AndReturn({'items': [], 'etag': '"new etag"'})

    self.mox.ReplayAll()
    self.post_task()

    source = self.sources[0].key.get()
    self.assertEqual('"new etag"', source.last_activities_etag)
    # reset etag back to None for the next tests
    source._set('etag', None)

  def test_last_activity_id(self):
    """We should store the last activity id seen and then send it as min_id."""
    self.sources[0].set_activities(list(reversed(self.activities)))
    self.post_task()

    source = self.sources[0].key.get()
    self.assertEqual('c', source.last_activity_id)
    source.last_polled = util.EPOCH
    source.put()

    self.mox.StubOutWithMock(FakeSource, 'get_activities_response')
    FakeSource.get_activities_response(
      count=mox.IgnoreArg(), fetch_replies=True, fetch_likes=True,
      fetch_shares=True, etag=None, min_id='c', cache=mox.IgnoreArg(),
      ).AndReturn({'items': []})

    self.mox.ReplayAll()
    self.post_task()

  def test_last_activity_id_not_tag_uri(self):
    self.activities[0]['id'] = 'a'
    self.activities[1]['id'] = 'b'
    self.activities[2]['id'] = 'c'
    self.sources[0].set_activities(list(reversed(self.activities)))
    self.post_task()

    source = self.sources[0].key.get()
    self.assertEqual('c', source.last_activity_id)

  def test_cache_trims_to_returned_activity_ids(self):
    """We should trim last_activities_cache_json to just the returned activity ids."""
    self.sources[0].last_activities_cache_json = json.dumps(
      {1: 2, 'x': 'y', 'prefix x': 1, 'prefix b': 0})
    self.post_task()

    source = self.sources[0].key.get()
    self.assert_equals({'prefix b': 0}, json.loads(source.last_activities_cache_json))

  def test_slow_poll_never_sent_webmention(self):
    self.sources[0].created = NOW - (FakeSource.FAST_POLL_GRACE_PERIOD +
                                     datetime.timedelta(minutes=1))
    self.sources[0].put()
    self.post_task()
    self.assert_task_eta(FakeSource.SLOW_POLL)

  def test_slow_poll_sent_webmention_over_month_ago(self):
    self.sources[0].created = NOW - (FakeSource.FAST_POLL_GRACE_PERIOD +
                                     datetime.timedelta(minutes=1))
    self.sources[0].last_webmention_sent = NOW - datetime.timedelta(days=32)
    self.sources[0].put()
    self.post_task()
    self.assert_task_eta(FakeSource.SLOW_POLL)

  def test_fast_poll_grace_period(self):
    self.sources[0].created = NOW - datetime.timedelta(minutes=1)
    self.sources[0].put()
    self.post_task()
    self.assert_task_eta(FakeSource.FAST_POLL)

  def test_fast_poll_hgr_sent_webmention(self):
    self.sources[0].created = NOW - (FakeSource.FAST_POLL_GRACE_PERIOD +
                                     datetime.timedelta(minutes=1))
    self.sources[0].last_webmention_sent = NOW - datetime.timedelta(days=1)
    self.sources[0].put()
    self.post_task()
    self.assert_task_eta(FakeSource.FAST_POLL)

  def test_set_last_syndication_url(self):
    """A successful posse-post-discovery round should set
    Source.last_syndication_url to approximately the current time.
    """
    self.sources[0].domain_urls = ['http://author']
    FakeGrSource.DOMAIN = 'source'
    self.sources[0].last_syndication_url = None
    self.sources[0].put()

    # leave at least one new response to trigger PPD
    for r in self.responses[:-1]:
      r.status = 'complete'
      r.put()

    self.expect_requests_get('http://author', """
    <html class="h-feed">
      <a class="h-entry" href="/permalink"></a>
    </html>""")

    self.expect_requests_get('http://author/permalink', """
    <html class="h-entry">
      <a class="u-url" href="http://author/permalink"></a>
      <a class="u-syndication" href="http://source/post/url"></a>
    </html>""")

    # refetch h-feed now that last_syndication_url should be set
    self.expect_requests_get('http://author', '')

    self.mox.ReplayAll()
    self.post_task()

    # query source
    source = self.sources[0].key.get()
    self.assertIsNotNone(source)
    self.assertIsNotNone(source.last_syndication_url)

  def test_do_not_refetch_hfeed(self):
    """Only 1 hour has passed since we last re-fetched the user's h-feed. Make
    Sure it is not fetched again"""
    self.sources[0].domain_urls = ['http://author']
    FakeGrSource.DOMAIN = 'source'
    self.sources[0].last_syndication_url = NOW - datetime.timedelta(minutes=10)
    # too recent to fetch again
    self.sources[0].last_hfeed_fetch = NOW - datetime.timedelta(hours=1)
    self.sources[0].put()

    # pretend we've already done posse-post-discovery for the source
    # and checked this permalink and found no back-links
    models.SyndicatedPost(parent=self.sources[0].key, original=None,
                          syndication='https://source/post/url').put()
    models.SyndicatedPost(parent=self.sources[0].key,
                          original='http://author/permalink',
                          syndication=None).put()

    # and all the status have already been sent
    for r in self.responses:
      r.status = 'complete'
      r.put()

    self.mox.ReplayAll()
    self.post_task()

    # should still be a blank SyndicatedPost
    relationships = models.SyndicatedPost.query(
      models.SyndicatedPost.original == 'http://author/permalink',
      ancestor=self.sources[0].key).fetch()
    self.assertEqual(1, len(relationships))
    self.assertIsNone(relationships[0].syndication)

    # should not repropagate any responses
    self.assertEquals(0, len(self.taskqueue_stub.GetTasks('propagate')))

  def test_do_refetch_hfeed(self):
    """Emulate a situation where we've done posse-post-discovery earlier and
    found no rel=syndication relationships for a particular silo URL. Every
    two hours or so, we should refetch the author's page and check to see if
    any new syndication links have been added or updated.
    """
    self.sources[0].domain_urls = ['http://author']
    FakeGrSource.DOMAIN = 'source'
    self.sources[0].last_syndication_url = NOW - datetime.timedelta(minutes=10)
    self.sources[0].last_hfeed_fetch = NOW - datetime.timedelta(hours=2,
                                                                minutes=10)
    self.sources[0].put()

    # pretend we've already done posse-post-discovery for the source
    # and checked this permalink and found no back-links
    models.SyndicatedPost(parent=self.sources[0].key, original=None,
                          syndication='https://source/post/url').put()
    models.SyndicatedPost(parent=self.sources[0].key,
                          original='http://author/permalink',
                          syndication=None).put()

    # and all the status have already been sent
    for r in self.responses:
      r.status = 'complete'
      r.put()

    self.expect_requests_get('http://author', """
    <html class="h-feed">
      <a class="h-entry" href="/permalink"></a>
    </html>""")

    self.expect_requests_get('http://author/permalink', """
    <html class="h-entry">
      <a class="u-url" href="http://author/permalink"></a>
      <a class="u-syndication" href="http://source/post/url"></a>
    </html>""")

    self.mox.ReplayAll()
    self.post_task()

    # should have a new SyndicatedPost
    relationships = models.SyndicatedPost.query(
      models.SyndicatedPost.original == 'http://author/permalink',
      ancestor=self.sources[0].key).fetch()
    self.assertEquals(1, len(relationships))
    self.assertEquals('https://source/post/url', relationships[0].syndication)

    # should repropagate all 9 responses
    tasks = self.taskqueue_stub.GetTasks('propagate')
    self.assertEquals(9, len(tasks))

    # and they should be in reverse creation order
    response_keys = [resp.key.urlsafe() for resp in self.responses]
    response_keys.reverse()
    task_keys = [testutil.get_task_params(task)['response_key']
                 for task in tasks]
    self.assertEquals(response_keys, task_keys)

  def test_no_duplicate_syndicated_posts(self):
    def assert_syndicated_posts(syndicated_posts, original, syndication):
      logging.debug('checking syndicated posts [%s -> %s] = %s',
                    original, syndication, syndicated_posts)
      self.assertEquals(1, len(syndicated_posts))
      self.assertEquals(original, syndicated_posts[0].original)
      self.assertEquals(syndication, syndicated_posts[0].syndication)

    class FakeGrSource_Instagram(testutil.FakeGrSource):
      DOMAIN = 'instagram'

    self.sources[0].domain_urls = ['http://author']
    self.sources[0].GR_CLASS = FakeGrSource_Instagram
    self.sources[0].last_syndication_url = util.EPOCH
    self.sources[0].last_hfeed_fetch = NOW

    for act in self.activities:
      act['object']['url'] = 'http://instagram/post/url'
      act['object']['content'] = 'instagram post'

    self.sources[0].put()

    class FakeGrSource_Twitter(testutil.FakeGrSource):
      DOMAIN = 'twitter'

    self.sources[1].domain_urls = ['http://author']
    self.sources[1].GR_CLASS = FakeGrSource_Twitter
    self.sources[1].last_syndication_url = util.EPOCH
    self.sources[1].last_hfeed_fetch = NOW
    twitter_acts = copy.deepcopy(self.activities)
    self.sources[1].set_activities(twitter_acts)

    for act in twitter_acts:
      act['object']['url'] = 'http://twitter/post/url'
      act['object']['content'] = 'twitter post'
      act['object']['replies']['items'][0]['content'] = '@-reply'

    self.sources[1].put()

    for _ in range(2):
      self.expect_requests_get('http://author', """
      <html class="h-feed">
        <a class="h-entry" href="/permalink"></a>
      </html>""")

      self.expect_requests_get('http://author/permalink', """
      <html class="h-entry">
        <a class="u-url" href="http://author/permalink"></a>
        <a class="u-syndication" href="http://instagram/post/url"></a>
      </html>""")

    self.mox.ReplayAll()
    for source in self.sources:
      self.post_task(source=source)

    assert_syndicated_posts(
      models.SyndicatedPost.query(
        models.SyndicatedPost.original == 'http://author/permalink',
        ancestor=self.sources[0].key).fetch(),
      'http://author/permalink', 'https://instagram/post/url')

    assert_syndicated_posts(
      models.SyndicatedPost.query(
        models.SyndicatedPost.syndication == 'https://instagram/post/url',
        ancestor=self.sources[0].key).fetch(),
      'http://author/permalink', 'https://instagram/post/url')

    assert_syndicated_posts(
      models.SyndicatedPost.query(
        models.SyndicatedPost.original == 'http://author/permalink',
        ancestor=self.sources[1].key).fetch(),
      'http://author/permalink', None)

    assert_syndicated_posts(
      models.SyndicatedPost.query(
        models.SyndicatedPost.syndication == 'https://twitter/post/url',
        ancestor=self.sources[1].key).fetch(),
      None, 'https://twitter/post/url')

    self.mox.VerifyAll()
    self.mox.UnsetStubs()

    for method in ('get', 'head', 'post'):
      self.mox.StubOutWithMock(requests, method, use_mock_anything=True)

    # force refetch h-feed to find the twitter link
    for source in self.sources:
      source.last_polled = util.EPOCH
      source.last_hfeed_fetch = NOW - datetime.timedelta(days=1)
      source.put()

    # instagram source fetches
    self.expect_requests_get('http://author', """
    <html class="h-feed">
      <a class="h-entry" href="/permalink"></a>
    </html>""")

    self.expect_requests_get('http://author/permalink', """
    <html class="h-entry">
      <a class="u-syndication" href="http://instagram/post/url"></a>
    </html>""")

    # refetch should find a twitter link this time
    self.expect_requests_get('http://author', """
    <html class="h-feed">
      <a class="h-entry" href="/permalink"></a>
    </html>""")

    self.expect_requests_get('http://author/permalink', """
    <html class="h-entry">
      <a class="u-url" href="http://author/permalink"></a>
      <a class="u-syndication" href="http://instagram/post/url"></a>
      <a class="u-syndication" href="http://twitter/post/url"></a>
    </html>""")

    self.mox.ReplayAll()
    for source in self.sources:
      self.post_task(source=source)

    assert_syndicated_posts(
      models.SyndicatedPost.query(
        models.SyndicatedPost.original == 'http://author/permalink',
        ancestor=self.sources[0].key).fetch(),
      'http://author/permalink', 'https://instagram/post/url')

    assert_syndicated_posts(
      models.SyndicatedPost.query(
        models.SyndicatedPost.syndication == 'https://instagram/post/url',
        ancestor=self.sources[0].key).fetch(),
      'http://author/permalink', 'https://instagram/post/url')

    assert_syndicated_posts(
      models.SyndicatedPost.query(
        models.SyndicatedPost.original == 'http://author/permalink',
        ancestor=self.sources[1].key).fetch(),
      'http://author/permalink', 'https://twitter/post/url')

    assert_syndicated_posts(
      models.SyndicatedPost.query(
        models.SyndicatedPost.syndication == 'https://twitter/post/url',
        ancestor=self.sources[1].key).fetch(),
      'http://author/permalink', 'https://twitter/post/url')

  def test_response_changed(self):
    """If a response changes, we should repropagate it from scratch.
    """
    source = self.sources[0]
    activity = self.activities[0]

    # just one response: self.responses[0]
    tags = activity['object']['tags']
    del activity['object']['tags']
    source.set_activities([activity])

    # first change to response
    self._change_response_and_poll()

    # second change to response
    self._change_response_and_poll()

    # return new response *and* existing response. both should be stored in
    # Source.seen_responses_cache_json
    replies = activity['object']['replies']['items']
    replies.append(self.activities[1]['object']['replies']['items'][0])

    self.post_task(reset=True)
    del replies[0]['activities']
    self.assert_equals(replies, json.loads(source.key.get().seen_responses_cache_json))
    self.responses[3].key.delete()

    # new responses that don't include existing response. cache will have
    # existing response.
    del activity['object']['replies']
    activity['object']['tags'] = tags

    self.post_task(reset=True)
    self.assert_equals([r.key for r in self.responses[:3]],
                       list(models.Response.query().iter(keys_only=True)))
    self.assert_equals(tags, json.loads(source.key.get().seen_responses_cache_json))

  def _change_response_and_poll(self):
    resp = self.responses[0].key.get() or self.responses[0]
    old_resp_jsons = resp.old_response_jsons + [resp.response_json]
    targets = resp.sent = resp.unsent
    resp.unsent = []
    resp.status = 'complete'
    resp.put()

    reply = self.activities[0]['object']['replies']['items'][0]
    reply['content'] += ' xyz'
    new_resp_json = json.dumps(reply)
    self.post_task(reset=True)

    resp = resp.key.get()
    self.assertEqual(new_resp_json, resp.response_json)
    self.assertEqual(old_resp_jsons, resp.old_response_jsons)
    self.assertEqual('new', resp.status)
    self.assertEqual(targets, resp.unsent)
    self.assertEqual([], resp.sent)

    tasks = self.taskqueue_stub.GetTasks('propagate')
    self.assertEquals(1, len(tasks))
    self.assertEquals(resp.key.urlsafe(),
                      testutil.get_task_params(tasks[0])['response_key'])
    self.taskqueue_stub.FlushQueue('propagate')

    source = self.sources[0].key.get()
    self.assert_equals([reply], json.loads(source.seen_responses_cache_json))


class PropagateTest(TaskQueueTest):

  post_url = '/_ah/queue/propagate'

  def setUp(self):
    super(PropagateTest, self).setUp()
    for r in self.responses[:3]:
      r.put()
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
                        error=None, input_endpoint=None, discovered_endpoint=None):
    if source_url is None:
      source_url = 'http://localhost/comment/fake/%s/a/1_2_a' % \
          self.sources[0].key.string_id()
    mock_send = send.WebmentionSend(source_url, target, endpoint=input_endpoint)
    mock_send.source_url = source_url
    mock_send.target_url = target
    mock_send.receiver_endpoint = (discovered_endpoint if discovered_endpoint
                                   else input_endpoint if input_endpoint
                                   else 'http://webmention/endpoint')
    mock_send.response = 'used in logging'
    mock_send.error = error
    return mock_send.send(timeout=999, headers=util.USER_AGENT_HEADER)

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
      global NOW
      NOW += datetime.timedelta(hours=1)
      self.post_task(response=r)
      self.assert_response_is('complete', NOW + LEASE_LENGTH,
                              sent=['http://target1/post/url'], response=r)
      self.assert_equals(NOW, self.sources[0].key.get().last_webmention_sent)
      memcache.flush_all()

  def test_propagate_from_error(self):
    """A normal propagate task, with a response starting as 'error'."""
    self.responses[0].status = 'error'
    self.responses[0].put()

    self.expect_webmention().AndReturn(True)
    self.mox.ReplayAll()
    self.post_task()
    self.assert_response_is('complete', NOW + LEASE_LENGTH,
                            sent=['http://target1/post/url'])
    self.assert_equals(NOW, self.sources[0].key.get().last_webmention_sent)

  def test_success_and_errors(self):
    """We should send webmentions to the unsent and error targets."""
    self.responses[0].unsent = ['http://1', 'http://2', 'http://3', 'http://8']
    self.responses[0].error = ['http://4', 'http://5', 'http://6']
    self.responses[0].sent = ['http://7']
    self.responses[0].put()

    self.expect_webmention(target='http://1').InAnyOrder().AndReturn(True)
    self.expect_webmention(target='http://2', error={'code': 'NO_ENDPOINT'})\
        .InAnyOrder().AndReturn(False)
    self.expect_webmention(target='http://3', error={'code': 'RECEIVER_ERROR'})\
        .InAnyOrder().AndReturn(False)
    self.expect_webmention(target='http://4',  # 4XX should go into 'failed'
                           error={'code': 'BAD_TARGET_URL', 'http_status': 404})\
        .InAnyOrder().AndReturn(False)
    self.expect_webmention(target='http://5',
                           error={'code': 'RECEIVER_ERROR', 'http_status': 403})\
        .InAnyOrder().AndReturn(False)
    self.expect_webmention(target='http://6',  # 5XX should go into 'error'
                           error={'code': 'BAD_TARGET_URL', 'http_status': 500})\
        .InAnyOrder().AndReturn(False)
    self.expect_webmention(target='http://8',  # 204 should go into 'skipped'
                           error={'code': 'BAD_TARGET_URL', 'http_status': 204})\
        .InAnyOrder().AndReturn(False)

    self.mox.ReplayAll()
    self.post_task(expected_status=ERROR_HTTP_RETURN_CODE)
    self.assert_response_is('error',
                            sent=['http://7', 'http://1'],
                            error=['http://3', 'http://6'],
                            failed=['http://4', 'http://5'],
                            skipped=['http://2', 'http://8'])
    self.assertEquals(NOW, self.sources[0].key.get().last_webmention_sent)

  def test_cached_webmention_discovery(self):
    """Webmention endpoints should be cached."""
    self.expect_webmention().AndReturn(True)
    # second webmention should use the cached endpoint
    self.expect_webmention(input_endpoint='http://webmention/endpoint'
                           ).AndReturn(True)

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

  def test_cached_webmention_discovery_shouldnt_refresh_cache(self):
    """A cached webmention discovery shouldn't be written back to the cache."""
    # first wm discovers and finds no endpoint, second uses cache, third rediscovers
    self.expect_webmention(error={'code': 'NO_ENDPOINT'}).AndReturn(False)
    self.expect_webmention().AndReturn(True)
    self.mox.ReplayAll()

    # inject a fake time.time into the memcache stub.
    #
    # ideally i'd do this:
    #
    #   self.testbed.init_memcache_stub(gettime=time_fn)
    #
    # but testbed doesn't pass kwargs to the memcache stub ctor like it does for
    # most other stubs. :( i started to file a patch against the app engine SDK,
    # but i eventually got impatient and gave up. background:
    # https://code.google.com/p/googleappengine/
    # https://code.google.com/p/googleappengine/issues/list?can=1&q=patch&sort=-id
    now = time.time()
    self.testbed.get_stub('memcache')._gettime = lambda: now

    self.post_task()
    self.assert_response_is('complete', skipped=['http://target1/post/url'])

    now += tasks.WEBMENTION_DISCOVERY_CACHE_TIME - 1
    self.responses[0].status = 'new'
    self.responses[0].put()
    self.post_task()
    self.assert_response_is('complete', skipped=['http://target1/post/url'])

    now += 2
    self.responses[0].status = 'new'
    self.responses[0].put()
    self.post_task()
    self.assert_response_is('complete', sent=['http://target1/post/url'])

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

    self.expect_requests_head('http://not/html', content_type='application/mpeg')

    self.mox.ReplayAll()
    self.post_task()
    self.assert_response_is('complete')

  def test_non_html_file(self):
    """If our HEAD fails, we should still require content-type text/html."""
    self.mox.UnsetStubs()  # drop WebmentionSend mock; let it run
    super(PropagateTest, self).setUp()

    self.responses[0].unsent = ['http://not/html']
    self.responses[0].put()
    self.expect_requests_head('http://not/html', status_code=405)
    self.expect_requests_get('http://not/html', content_type='image/gif',
                             timeout=999, verify=False)

    self.mox.ReplayAll()
    self.post_task()
    self.assert_response_is('complete', skipped=['http://not/html'])

  def test_non_html_file_extension(self):
    """If our HEAD fails, we should infer type from file extension."""
    self.responses[0].unsent = ['http://this/is/a.pdf']
    self.responses[0].put()

    self.expect_requests_head('http://this/is/a.pdf', status_code=405,
                              # we should ignore an error response's content type
                              content_type='text/html')

    self.mox.ReplayAll()
    self.post_task()
    self.assert_response_is('complete')

  def test_content_type_html_with_charset(self):
    """We should handle Content-Type: text/html; charset=... ok."""
    self.mox.UnsetStubs()  # drop WebmentionSend mock; let it run
    super(PropagateTest, self).setUp()

    self.responses[0].unsent = ['http://html/charset']
    self.responses[0].put()
    self.expect_requests_head('http://html/charset', status_code=405)
    self.expect_requests_get(
      'http://html/charset',
      content_type='text/html; charset=utf-8',
      response_headers={'Link': '<http://my/endpoint>; rel="webmention"'},
      timeout=999, verify=False)

    source_url = ('http://localhost/comment/fake/%s/a/1_2_a' %
                  self.sources[0].key.string_id())
    self.expect_requests_post(
      'http://my/endpoint',
      data={'source': source_url, 'target': 'http://html/charset'},
      timeout=999, verify=False)

    self.mox.ReplayAll()
    self.post_task()
    self.assert_response_is('complete', sent=['http://html/charset'])

  def test_no_content_type_header(self):
    """If the Content-Type header is missing, we should assume text/html."""
    self.mox.UnsetStubs()  # drop WebmentionSend mock; let it run
    super(PropagateTest, self).setUp()

    self.responses[0].unsent = ['http://unknown/type']
    self.responses[0].put()
    self.expect_requests_head('http://unknown/type', status_code=405)
    self.expect_requests_get('http://unknown/type', content_type=None,
                             timeout=999, verify=False)

    self.mox.ReplayAll()
    self.post_task()
    self.assert_response_is('complete', skipped=['http://unknown/type'])

  def test_no_targets(self):
    """No target URLs."""
    self.responses[0].unsent = []
    self.responses[0].put()

    self.mox.ReplayAll()
    self.post_task()
    self.assert_response_is('complete', NOW + LEASE_LENGTH)

  def test_unicode_in_target_url(self):
    """Target URLs with escaped unicode chars should work ok.
    Background: https://github.com/snarfed/bridgy/issues/248
    """
    url = 'https://maps/?q=' + urllib.quote_plus('3 Cours de la République')
    self.responses[0].unsent = [url]
    self.responses[0].put()

    self.expect_webmention(target=url).AndReturn(True)
    self.mox.ReplayAll()

    self.post_task()
    self.assert_response_is('complete', sent=[url])

  def test_already_complete(self):
    """If the response has already been propagated, do nothing."""
    self.responses[0].status = 'complete'
    self.responses[0].put()

    self.post_task()
    self.assert_response_is('complete', unsent=['http://target1/post/url'])

  def test_set_webmention_endpoint(self):
    """Should set Source.webmention_endpoint if it's unset."""
    self.responses[0].unsent = ['http://bar/1', 'http://foo/2']
    self.responses[0].put()

    self.assertIsNone(self.sources[0].webmention_endpoint)
    self.sources[0].domains = ['foo']
    self.sources[0].put()

    # target isn't in source.domains
    self.expect_webmention(target='http://bar/1', discovered_endpoint='no'
                           ).AndReturn(True)
    # target is in source.domains
    self.expect_webmention(target='http://foo/2', discovered_endpoint='yes'
                           ).AndReturn(True)

    self.mox.ReplayAll()
    self.post_task()
    self.assert_equals('yes', self.sources[0].key.get().webmention_endpoint)

  def test_leased(self):
    """If the response is processing and the lease hasn't expired, do nothing."""
    self.responses[0].status = 'processing'
    leased_until = NOW + datetime.timedelta(minutes=1)
    self.responses[0].leased_until = leased_until
    self.responses[0].put()

    self.post_task(expected_status=ERROR_HTTP_RETURN_CODE)
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
    self.assert_response_is('complete', NOW + LEASE_LENGTH,
                           sent=['http://target1/post/url'])

  def test_no_response(self):
    """If the response doesn't exist, the request should fail."""
    self.responses[0].key.delete()
    self.post_task(expected_status=ERROR_HTTP_RETURN_CODE)

  def test_no_source(self):
    """If the source doesn't exist, the request should give up."""
    self.sources[0].key.delete()
    self.post_task(expected_status=200)

  def test_non_public_activity(self):
    """If the activity is non-public, we should give up."""
    activity = json.loads(self.responses[0].activities_json[0])
    activity['to'] = [{'objectType':'group', 'alias':'@private'}]
    self.responses[0].activities_json = [json.dumps(activity)]
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
      expected_status = 200 if give_up else ERROR_HTTP_RETURN_CODE
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
    self.post_task(expected_status=ERROR_HTTP_RETURN_CODE)
    self.assert_response_is('error', None, error=['http://first'],
                           sent=['http://second'])
    self.assert_equals(NOW, self.sources[0].key.get().last_webmention_sent)

  def test_webmention_exception(self):
    """Exceptions on individual target URLs shouldn't stop the whole task."""
    self.responses[0].unsent = ['http://error', 'http://good']
    self.responses[0].put()
    self.expect_webmention(target='http://error').AndRaise(Exception('foo'))
    self.expect_webmention(target='http://good').AndReturn(True)
    self.mox.ReplayAll()

    self.post_task(expected_status=ERROR_HTTP_RETURN_CODE)
    self.assert_response_is('error', None, error=['http://error'],
                            sent=['http://good'])
    self.assert_equals(NOW, self.sources[0].key.get().last_webmention_sent)

  def test_dns_failure(self):
    """If DNS lookup fails for a URL, we should give up.
    https://github.com/snarfed/bridgy/issues/254
    """
    self.responses[0].put()
    self.expect_webmention().AndRaise(requests.exceptions.ConnectionError(
        'Max retries exceeded: DNS lookup failed for URL: foo'))
    self.mox.ReplayAll()

    self.post_task()
    self.assert_response_is('complete', failed=['http://target1/post/url'])

  def test_redirect_to_too_long_url(self):
    """If a URL redirects to one over the URL length limit, we should skip it.

    https://github.com/snarfed/bridgy/issues/273
    """
    too_long = 'http://host/' + 'x' * _MAX_STRING_LENGTH
    self.expect_requests_head('http://target1/post/url', redirected_url=too_long)
    self.mox.ReplayAll()

    self.post_task()
    self.assert_response_is('complete', failed=['http://target1/post/url'])

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

  def test_activity_id_not_tag_uri(self):
    """If the activity id isn't a tag uri, we should just use it verbatim."""
    activity = json.loads(self.responses[0].activities_json[0])
    activity['id'] = 'AAA'
    self.responses[0].activities_json = [json.dumps(activity)]

    self.responses[0].unsent = ['http://good']
    self.responses[0].put()

    source_url = 'https://brid-gy.appspot.com/comment/fake/%s/AAA/1_2_a' % \
        self.sources[0].key.string_id()
    self.expect_webmention(source_url=source_url, target='http://good')\
        .AndReturn(True)

    self.mox.ReplayAll()
    self.post_task(base_url='http://www.brid.gy')

  def test_response_with_multiple_activities(self):
    """Should use Response.urls_to_activity to generate the source URLs.
    """
    self.responses[0].activities_json = [
      '{"id": "000"}', '{"id": "111"}', '{"id": "222"}']
    self.responses[0].unsent = ['http://AAA', 'http://BBB', 'http://CCC']
    self.responses[0].urls_to_activity = json.dumps(
      {'http://AAA': 0, 'http://BBB': 1, 'http://CCC': 2})
    self.responses[0].put()

    source_url = 'https://brid-gy.appspot.com/comment/fake/%s/%%s/1_2_a' % \
        self.sources[0].key.string_id()
    self.expect_webmention(source_url=source_url % '000', target='http://AAA')\
        .AndReturn(True)
    self.expect_webmention(source_url=source_url % '111', target='http://BBB')\
        .AndReturn(True)
    self.expect_webmention(source_url=source_url % '222', target='http://CCC')\
        .AndReturn(True)

    self.mox.ReplayAll()
    self.post_task(base_url='http://www.brid.gy')

  def test_complete_exception(self):
    """If completing raises an exception, the lease should be released."""
    self.expect_webmention().AndReturn(True)
    self.mox.StubOutWithMock(PropagateResponse, 'complete')
    PropagateResponse.complete().AndRaise(Exception('foo'))
    self.mox.ReplayAll()

    self.post_task(expected_status=500)
    self.assert_response_is('error', None, sent=['http://target1/post/url'])

  def test_source_url_key_error(self):
    """We should gracefully retry when we hit the KeyError bug.

    https://github.com/snarfed/bridgy/issues/237
    """
    self.responses[0].urls_to_activity = json.dumps({'bad': 9})
    self.responses[0].put()

    self.mox.ReplayAll()
    self.post_task(expected_status=PropagateResponse.ERROR_HTTP_RETURN_CODE)

  def test_propagate_blogpost(self):
    """Blog post propagate task."""
    source_key = FakeSource.new(None, domains=['fake']).put()
    links = ['http://fake/post', '/no/domain', 'http://ok/one.png',
             'http://ok/two', 'http://ok/two', # repeated
             ]
    blogpost = models.BlogPost(id='x', source=source_key, unsent=links)
    blogpost.put()

    self.expect_requests_head('http://fake/post')
    self.expect_requests_head('http://ok/one.png', content_type='image/png')
    self.expect_requests_head('http://ok/two')
    self.expect_webmention(source_url='x', target='http://ok/two').AndReturn(True)
    self.mox.ReplayAll()

    self.post_url = '/_ah/queue/propagate-blogpost'
    super(PropagateTest, self).post_task(params={'key': blogpost.key.urlsafe()})
    self.assert_response_is('complete', NOW + LEASE_LENGTH,
                            sent=['http://ok/two'], response=blogpost)
    self.assert_equals(NOW, source_key.get().last_webmention_sent)

  def test_propagate_blogpost_allows_bridgy_publish_links(self):
    source_key = FakeSource.new(None, domains=['fake']).put()
    blogpost = models.BlogPost(id='x', source=source_key,
                               unsent=['https://www.brid.gy/publish/facebook'])
    blogpost.put()

    self.expect_requests_head('https://www.brid.gy/publish/facebook')
    self.expect_webmention(
      source_url='x',
      target='https://www.brid.gy/publish/facebook',
      discovered_endpoint='https://www.brid.gy/publish/webmention',
      ).AndReturn(True)
    self.mox.ReplayAll()

    self.post_url = '/_ah/queue/propagate-blogpost'
    super(PropagateTest, self).post_task(params={'key': blogpost.key.urlsafe()})
    self.assert_response_is('complete', response=blogpost,
                            sent=['https://www.brid.gy/publish/facebook'])

  def test_propagate_blogpost_follows_redirects_before_checking_self_link(self):
    source_key = FakeSource.new(None, domains=['fake']).put()
    blogpost = models.BlogPost(id='x', source=source_key,
                               unsent=['http://will/redirect'])
    blogpost.put()

    self.expect_requests_head('http://will/redirect',
                              redirected_url='http://www.fake/self/link')
    self.mox.ReplayAll()

    self.post_url = '/_ah/queue/propagate-blogpost'
    super(PropagateTest, self).post_task(params={'key': blogpost.key.urlsafe()})
    self.assert_response_is('complete', response=blogpost)
