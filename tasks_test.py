"""Unit tests for tasks.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import copy
import datetime
import json
import logging
import mox
import StringIO
import urllib
import urllib2
import urlparse

import apiclient
from google.appengine.api import memcache
from google.appengine.ext import ndb
import httplib2
from oauth2client.client import AccessTokenRefreshError
from python_instagram.bind import InstagramAPIError
import requests

from appengine_config import HTTP_TIMEOUT
import models
import models_test
import tasks
from tasks import PropagateResponse
import testutil
from testutil import FakeSource
import util
from webmentiontools import send

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

  def post_task(self, expected_status=200, source=None):
    super(PollTest, self).post_task(
      expected_status=expected_status,
      params={'source_key': (source or self.sources[0]).key.urlsafe(),
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
      delta: datetime.timedelta
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
      for err in (urllib2.HTTPError('url', 401, 'msg', {},  StringIO.StringIO('body')),
                  InstagramAPIError('400', 'OAuthAccessTokenException', 'foo'),
                  AccessTokenRefreshError('invalid_grant'),
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
      for err in (InstagramAPIError('503', 'Rate limited', '...'),
                  apiclient.errors.HttpError(httplib2.Response({'status': 429}), ''),
                  urllib2.HTTPError('url', 403, 'msg', {}, None)):
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

    self.mox.StubOutWithMock(FakeSource, 'get_activities_response')
    FakeSource.get_activities_response(
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

  def test_slow_poll(self):
    # grace period has passed, hasn't sent webmention
    self.sources[0].created = NOW - (FakeSource.FAST_POLL_GRACE_PERIOD +
                                     datetime.timedelta(minutes=1))
    self.sources[0].put()
    self.post_task()
    self.assert_task_eta(FakeSource.SLOW_POLL)

  def test_fast_poll_grace_period(self):
    self.sources[0].created = NOW - datetime.timedelta(minutes=1)
    self.sources[0].put()
    self.post_task()
    self.assert_task_eta(FakeSource.FAST_POLL)

  def test_fast_poll_has_sent_webmention(self):
    self.sources[0].created = NOW - (FakeSource.FAST_POLL_GRACE_PERIOD +
                                     datetime.timedelta(minutes=1))
    self.sources[0].last_webmention_sent = NOW - datetime.timedelta(days=100)
    self.sources[0].put()
    self.post_task()
    self.assert_task_eta(FakeSource.FAST_POLL)

  def test_set_last_syndication_url(self):
    """A successful posse-post-discovery round should set
    Source.last_syndication_url to approximately the current time.
    """
    self.sources[0].domain_urls = ['http://author']
    self.sources[0].AS_CLASS.DOMAIN = 'source'
    self.sources[0].last_syndication_url = None
    self.sources[0].put()

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
    self.sources[0].AS_CLASS.DOMAIN = 'source'
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
    relationship = models.SyndicatedPost.query_by_original(
        self.sources[0], 'http://author/permalink')
    self.assertIsNotNone(relationship)
    self.assertIsNone(relationship.syndication)

    # should not repropagate any responses
    self.assertEquals(0, len(self.taskqueue_stub.GetTasks('propagate')))

  def test_do_refetch_hfeed(self):
    """Emulate a situation where we've done posse-post-discovery earlier and
    found no rel=syndication relationships for a particular silo URL. Every
    two hours or so, we should refetch the author's page and check to see if
    any new syndication links have been added or updated.
    """
    self.sources[0].domain_urls = ['http://author']
    self.sources[0].AS_CLASS.DOMAIN = 'source'
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
    relationship = models.SyndicatedPost.query_by_original(
      self.sources[0], 'http://author/permalink')
    self.assertIsNotNone(relationship)
    self.assertEquals('https://source/post/url', relationship.syndication)

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

    class FakeAsSource_Instagram(testutil.FakeAsSource):
      DOMAIN = 'instagram'

    self.sources[0].domain_urls = ['http://author']
    self.sources[0].AS_CLASS = FakeAsSource_Instagram
    self.sources[0].last_syndication_url = util.EPOCH
    self.sources[0].last_hfeed_fetch = NOW

    for act in self.activities:
      act['object']['url'] = 'http://instagram/post/url'
      act['object']['content'] = 'instagram post'

    self.sources[0].put()

    class FakeAsSource_Twitter(testutil.FakeAsSource):
      DOMAIN = 'twitter'

    self.sources[1].domain_urls = ['http://author']
    self.sources[1].AS_CLASS = FakeAsSource_Twitter
    self.sources[1].last_syndication_url = util.EPOCH
    self.sources[1].last_hfeed_fetch = NOW
    twitter_acts = copy.deepcopy(self.activities)
    self.sources[1].set_activities(twitter_acts)

    for act in twitter_acts:
      act['object']['url'] = 'http://twitter/post/url'
      act['object']['content'] = 'twitter post'

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

    self.mox.UnsetStubs()
    self.mox.VerifyAll()

    for method in ('get', 'head', 'post'):
      self.mox.StubOutWithMock(requests, method, use_mock_anything=True)

    # force refetch h-feed to find the twitter link
    for source in self.sources:
      source.last_polled = util.EPOCH
      source.last_hfeed_fetch = NOW - datetime.timedelta(days=1)
      source.put()

    # instagram source won't refetch the permalink. nothing new to find
    self.expect_requests_get('http://author', """
    <html class="h-feed">
      <a class="h-entry" href="/permalink"></a>
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
      self.assert_response_is('complete', NOW + LEASE_LENGTH,
                              sent=['http://target1/post/url'], response=r)
      memcache.flush_all()

    self.assert_equals(NOW, self.sources[0].key.get().last_webmention_sent)

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
    self.post_task(expected_status=ERROR_HTTP_RETURN_CODE)
    response = self.responses[0].key.get()
    self.assert_response_is('error',
                            sent=['http://6', 'http://1'],
                            error=['http://3', 'http://5'],
                            failed=['http://4'],
                            skipped=['http://2'])
    self.assertIsNone(self.sources[0].key.get().last_webmention_sent)

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

    self.expect_requests_head('http://not/html', content_type='application/mpeg')

    self.mox.ReplayAll()
    self.post_task()
    self.assert_response_is('complete')

  def test_no_targets(self):
    """No target URLs."""
    self.responses[0].unsent = []
    self.responses[0].put()

    self.mox.ReplayAll()
    self.post_task()
    self.assert_response_is('complete', NOW + LEASE_LENGTH)

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

  def test_propagate_blogpost(self):
    """Blog post propagate task."""
    source_key = FakeSource.new(None, domains=['fake']).put()
    links = ['http://fake/post', '/no/domain', 'http://ok/one.png',
             'http://ok/two', 'http://ok/two', # repeated
             ]
    blogpost = models.BlogPost(id='x', source=source_key, unsent=links)
    blogpost.put()

    self.expect_requests_head('http://ok/two')
    self.expect_webmention(source_url='x', target='http://ok/two').AndReturn(True)
    self.expect_requests_head('http://ok/one.png', content_type='image/png')
    self.mox.ReplayAll()

    self.post_url = '/_ah/queue/propagate-blogpost'
    super(PropagateTest, self).post_task(
      expected_status=200,
      params={'key': blogpost.key.urlsafe()})
    self.assert_response_is('complete', NOW + LEASE_LENGTH,
                            sent=['http://ok/two'], response=blogpost)
    self.assert_equals(NOW, source_key.get().last_webmention_sent)
