# coding=utf-8
"""Unit tests for tasks.py.
"""
from __future__ import unicode_literals
from __future__ import absolute_import

from future.utils import native_str
from future import standard_library
standard_library.install_aliases()
from builtins import zip
import copy
import datetime
import http.client
import logging
import mox
import socket
import string
import io
import time
import urllib.request, urllib.parse, urllib.error

import apiclient
from cachetools import TTLCache
from google.appengine.ext import ndb
from google.appengine.ext.ndb.model import _MAX_STRING_LENGTH
import httplib2
from oauth_dropins.webutil.util import json_dumps, json_loads
from oauth2client.client import AccessTokenRefreshError
import requests
from webmentiontools import send

import appengine_config

import models
from models import Response, SyndicatedPost
from twitter import Twitter
import tasks
from tasks import PropagateResponse
from . import testutil
from .testutil import FakeSource, FakeGrSource, NOW
import util
from util import ERROR_HTTP_RETURN_CODE

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
    resp = tasks.application.get_response(
      self.post_url, method='POST',
      body=native_str(urllib.parse.urlencode(params)), **kwargs)
    self.assertEqual(expected_status, resp.status_int)

  def assert_responses(self, expected=None, ignore=tuple()):
    """Asserts that all of self.responses are saved."""
    if expected is None:
      expected = self.responses

    # sort fields in json properties since they're compared as strings
    stored = list(Response.query())
    for resp in expected + stored:
      if 'activities_json' not in ignore:
        resp.activities_json = [json_dumps(json_loads(a), sort_keys=True)
                                for a in resp.activities_json]
      if 'response_json' not in ignore:
        resp.response_json = json_dumps(json_loads(resp.response_json), sort_keys=True)

    self.assert_entities_equal(expected, stored,
                               ignore=('created', 'updated') + ignore)

  def expect_get_activities(self, **kwargs):
    """Adds and returns an expected get_activities_response() call."""
    self.mox.StubOutWithMock(FakeSource, 'get_activities_response')
    params = {
      'fetch_replies': True,
      'fetch_likes': True,
      'fetch_shares': True,
    }
    params.update(kwargs)
    return FakeSource.get_activities_response(**params)


class PollTest(TaskQueueTest):

  post_url = '/_ah/queue/poll'

  def setUp(self):
    super(PollTest, self).setUp()
    FakeGrSource.DOMAIN = 'source'
    appengine_config.DEBUG = True

  def tearDown(self):
    FakeGrSource.DOMAIN = 'fa.ke'
    appengine_config.DEBUG = False
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

  def expect_get_activities(self, **kwargs):
    """Adds and returns an expected get_activities_response() call."""
    full_kwargs = {
      'fetch_mentions': True,
      'count': mox.IgnoreArg(),
      'etag': None,
      'min_id': None,
      'cache': mox.IgnoreArg(),
    }
    full_kwargs.update(kwargs)
    return super(PollTest, self).expect_get_activities(**full_kwargs)

  def test_poll(self):
    """A normal poll task."""
    self.assertEqual(0, Response.query().count())
    self.assertEqual([], self.taskqueue_stub.GetTasks('poll'))

    self.post_task()
    self.assertEqual(12, Response.query().count())
    self.assert_responses()

    source = self.sources[0].key.get()
    self.assertEqual(NOW, source.last_polled)
    self.assertEqual('ok', source.poll_status)

    tasks = self.taskqueue_stub.GetTasks('propagate')
    for task in tasks:
      self.assertEqual('/_ah/queue/propagate', task['url'])
    keys = [ndb.Key(urlsafe=testutil.get_task_params(t)['response_key'])
            for t in tasks]
    self.assert_equals(keys, [r.key for r in self.responses])

    tasks = self.taskqueue_stub.GetTasks('poll')
    self.assertEqual(1, len(tasks))
    self.assertEqual('/_ah/queue/poll', tasks[0]['url'])
    self.assert_task_eta(FakeSource.FAST_POLL)
    params = testutil.get_task_params(tasks[0])
    self.assert_equals(source.key.urlsafe(), params['source_key'])

  def test_poll_status_polling(self):
    def check_poll_status(*args, **kwargs):
      self.assertEqual('polling', self.sources[0].key.get().poll_status)

    self.expect_get_activities().WithSideEffects(check_poll_status) \
                                .AndReturn({'items': []})
    self.mox.ReplayAll()
    self.post_task()
    self.assertEqual('ok', self.sources[0].key.get().poll_status)

  def test_poll_error(self):
    """If anything goes wrong, the source status should be set to 'error'."""
    self.expect_get_activities().AndRaise(Exception('foo'))
    self.mox.ReplayAll()

    self.assertRaises(Exception, self.post_task)
    self.assertEqual('error', self.sources[0].key.get().poll_status)
    self.assertEqual(0, len(self.taskqueue_stub.GetTasks('poll')))

  def test_poll_silo_500(self):
    """If a silo HTTP request 500s, we should quietly retry the task."""
    self.expect_get_activities().AndRaise(
      urllib.error.HTTPError('url', 505, 'msg', {}, None))
    self.mox.ReplayAll()

    self.post_task(expected_status=ERROR_HTTP_RETURN_CODE)
    self.assertEqual('error', self.sources[0].key.get().poll_status)

  def test_poll_silo_deadlines(self):
    """If a silo HTTP request deadlines, we should quietly retry the task."""
    self.expect_get_activities().AndRaise(
      urllib.error.URLError(socket.gaierror('deadlined')))
    self.mox.ReplayAll()

    self.post_task(expected_status=ERROR_HTTP_RETURN_CODE)
    self.assertEqual('error', self.sources[0].key.get().poll_status)

  def test_reset_status_to_enabled(self):
    """After a successful poll, status should be set to 'enabled' and 'ok'."""
    source = self.sources[0]
    source.status = 'error'
    source.poll_status = 'error'
    source.rate_limited = True
    source.put()

    self.post_task()
    source = source.key.get()
    self.assertEqual('enabled', source.status)
    self.assertEqual('ok', source.poll_status)
    self.assertFalse(source.rate_limited)

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
    FakeGrSource.activities = [self.activities[0]]

    self.post_task()
    expected = ['http://tar.get/%s' % i for i in ('a', 'b', 'c', 'd', 'e', 'f')]
    self.assert_equals(expected, self.responses[0].key.get().unsent)

  def test_original_post_discovery_dedupes(self):
    """Target URLs should be deduped, ignoring scheme and domain case insensitive."""
    # trigger posse post discovery
    self.sources[0].domain_urls = ['http://author']
    self.sources[0].put()

    SyndicatedPost(parent=self.sources[0].key,
                   original='http://Tar.Get/a',
                   syndication='https://fa.ke/post/url').put()

    self.activities[0]['object'].update({
      'tags': [{'objectType': 'article', 'url': 'https://tar.get/a'}],
      'attachments': [{'objectType': 'article', 'url': 'http://tar.get/a'}],
      'content': 'foo https://TAR.GET/a bar (tar.get a)',
    })
    FakeGrSource.activities = [self.activities[0]]

    self.post_task()
    self.assert_equals(['https://tar.get/a'], self.responses[0].key.get().unsent)

  def test_backfeed_requires_syndication_link(self):
    # trigger posse post discovery
    self.sources[0].domain_urls = ['http://author']
    self.sources[0].put()

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
    FakeGrSource.activities = [self.activities[0]]

    SyndicatedPost(parent=self.sources[0].key,
                   original='http://tar.get/z',
                   syndication='https://fa.ke/post/url').put()

    self.mox.stubs.Set(FakeSource, 'BACKFEED_REQUIRES_SYNDICATION_LINK', True)
    self.post_task()
    self.assert_equals(['http://tar.get/z'], self.responses[0].key.get().unsent)

  def test_non_html_url(self):
    """Target URLs that aren't HTML should be ignored."""
    obj = self.activities[0]['object']
    obj['tags'] = []
    obj['content'] = 'http://not/html'
    FakeGrSource.activities = [self.activities[0]]

    self.expect_requests_head('http://not/html', content_type='application/pdf')

    self.mox.ReplayAll()
    self.post_task()
    self.assert_equals([], self.responses[0].key.get().unsent)

  def test_resolved_url(self):
    """A URL that redirects should be resolved."""
    obj = self.activities[0]['object']
    obj['tags'] = []
    obj['content'] = 'http://will/redirect'
    FakeGrSource.activities = [self.activities[0]]

    self.expect_requests_head('http://will/redirect', redirected_url='http://final/url')

    self.mox.ReplayAll()
    self.post_task()
    self.assert_equals(['http://final/url'], self.responses[0].key.get().unsent)

  def test_resolve_url_fails(self):
    """A URL that fails to resolve should still be handled ok."""
    obj = self.activities[0]['object']
    obj['tags'] = []
    obj['content'] = 'http://fails/resolve'
    FakeGrSource.activities = [self.activities[0]]

    self.expect_requests_head('http://fails/resolve', status_code=400)

    self.mox.ReplayAll()
    self.post_task()
    self.assert_equals(['http://fails/resolve'],
                       self.responses[0].key.get().unsent)

  def test_non_html_file_extension(self):
    """If our HEAD request fails, we should infer type from file extension."""
    self.activities[0]['object'].update({'tags': [], 'content': 'http://x/a.zip'})
    FakeGrSource.activities = [self.activities[0]]

    self.expect_requests_head('http://x/a.zip', status_code=405,
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
    FakeGrSource.activities = [self.activities[0]]

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
    FakeGrSource.activities = [self.activities[0]]

    self.post_task()
    self.assert_equals(['http://foo/bar?a=b'],
                       self.responses[0].key.get().unsent)

  def test_strip_utm_query_params_after_redirect(self):
    """utm_* query params should also be stripped after redirects."""
    for a in self.activities:
      a['object']['tags'][0]['id'] = 'tag:source.com,2013:only_reply'
      del a['object']['tags'][1:], a['object']['replies']

    # test with two activities so we can check urls_to_activity.
    # https://github.com/snarfed/bridgy/issues/237
    self.activities[0]['object'].update({'content': 'http://redir/0'})
    self.activities[1]['object'].update({'content': 'http://redir/1'})
    FakeGrSource.activities = self.activities[0:2]

    self.expect_requests_head(
      'http://redir/0', redirected_url='http://first/?utm_medium=x').InAnyOrder()
    self.expect_requests_head(
      'http://redir/1', redirected_url='http://second/?utm_source=Twitter').InAnyOrder()
    self.mox.ReplayAll()
    self.post_task()

    self.assertEquals(1, Response.query().count())
    resp = Response.query().get()
    self.assert_equals(['http://first/', 'http://second/'], resp.unsent)
    self.assert_equals(['http://first/', 'http://second/'],
                       list(json_loads(resp.urls_to_activity).keys()))

  def test_too_long_urls(self):
    """URLs longer than the datastore's limit should be truncated and skipped.

    https://github.com/snarfed/bridgy/issues/273
    """
    self.activities[0]['object'].update({'tags': [], 'content': 'http://foo/bar'})
    FakeGrSource.activities = [self.activities[0]]

    too_long = 'http://host/' + 'x' * _MAX_STRING_LENGTH
    self.expect_requests_head('http://foo/bar', redirected_url=too_long)

    self.mox.ReplayAll()
    self.post_task()
    resp = self.responses[0].key.get()
    self.assert_equals([], resp.unsent)
    self.assert_equals([too_long[:_MAX_STRING_LENGTH - 4] + '...'], resp.failed)

  def test_non_public_posts(self):
    """Only posts without to: or with to: @public should be propagated."""
    del self.activities[0]['object']['to']

    self.activities[1]['object']['to'] = [{'objectType':'group', 'alias':'@private'}]
    now = testutil.NOW.replace(microsecond=0)
    self.activities[1]['published'] = now.isoformat()

    self.activities[2]['object']['to'] = [{'objectType':'group', 'alias':'@public'}]
    public_date = now - datetime.timedelta(weeks=1)
    self.activities[2]['published'] = public_date.isoformat()

    # Facebook returns 'unknown' for wall posts
    unknown = copy.deepcopy(self.activities[1])
    unknown['id'] = unknown['object']['id'] = 'x'
    unknown['object']['to'] = [{'objectType': 'unknown'}]
    self.activities.append(unknown)

    self.post_task()
    ids = set()
    for task in self.taskqueue_stub.GetTasks('propagate'):
      resp_key = ndb.Key(urlsafe=testutil.get_task_params(task)['response_key'])
      ids.update(json_loads(a)['id'] for a in resp_key.get().activities_json)
    self.assert_equals(ids, set([self.activities[0]['id'], self.activities[2]['id']]))

    source = self.sources[0].key.get()
    self.assertEquals(public_date, source.last_public_post)
    self.assertEquals(2, source.recent_private_posts)

  def test_no_responses(self):
    """Handle activities without responses ok.
    """
    activities = self.sources[0].get_activities()
    for a in activities:
      a['object'].update({'replies': {}, 'tags': []})
    FakeGrSource.activities = activities

    self.post_task()
    self.assert_equals([], list(Response.query()))

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
    fb_id_resp = Response(id='tag:facebook.com,2013:12:34:56_78',
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
      # prevent posse post discovery (except 2, below)
      del a['object']['url']
      del a['url']

    self.activities[1]['object'].update({
        'content': '',
        'attachments': [{'objectType': 'article', 'url': 'http://from/tag'}],
    })
    self.activities[2]['object'].update({
        'content': '',
        'url': 'https://fa.ke/2',
    })

    FakeGrSource.activities = self.activities

    # trigger posse post discovery
    self.sources[0].domain_urls = ['http://author']
    self.sources[0].put()
    SyndicatedPost(parent=self.sources[0].key,
                   original='http://from/synd/post',
                   syndication='https://fa.ke/2').put()

    self.post_task()
    self.assertEquals(1, len(self.taskqueue_stub.GetTasks('propagate')))
    self.assertEquals(1, Response.query().count())
    resp = Response.query().get()
    self.assert_equals(['tag:source.com,2013:%s' % id for id in ('a', 'b', 'c')],
                       [json_loads(a)['id'] for a in resp.activities_json])

    urls = ['http://from/tag', 'http://from/synd/post', 'http://target1/post/url']
    self.assert_equals(urls, resp.unsent)
    self.assert_equals(urls, list(json_loads(resp.urls_to_activity).keys()))

  def test_multiple_activities_no_target_urls(self):
    """Response.urls_to_activity should be left unset.
    """
    for a in self.activities:
      a['object']['replies']['items'][0]['id'] = 'tag:source.com,2013:only_reply'
      a['object']['tags'] = []
      del a['object']['url']  # prevent posse post discovery
      del a['object']['content']
    FakeGrSource.activities = self.activities

    self.post_task()
    resp = Response.query().get()
    self.assert_equals([], resp.unsent)
    self.assert_equals('complete', resp.status)
    self.assertIsNone(resp.urls_to_activity)

  def test_search_for_links_backfeed_posts_and_comments(self):
    """Search for links to the source's domains in posts.

    Backfeed those posts and their comments, but not likes, reposts, or rsvps.

    https://github.com/snarfed/bridgy/issues/456
    """
    source = self.sources[0]
    source.domain_urls = ['http://foo/', 'https://bar/baz?baj']
    source.domains = ['target1']
    source.put()

    # return one normal activity and one searched link
    activity = self.activities[0]
    # prevent posse post discovery
    del activity['object']['url']
    del activity['url']
    FakeGrSource.activities = [activity]

    reply = {
      'objectType': 'comment',
      'id': 'tag:source.com,2013:9_comment',
      'content': 'foo bar',
    }
    colliding_reply = copy.copy(self.activities[0]['object']['replies']['items'][0])
    colliding_reply['objectType'] = 'note'
    links = [{
       # good link
       'id': 'tag:source.com,2013:9',
       'object': {
         'objectType': 'note',
         'content': 'foo http://target9/post/url bar',
         'replies': {'items': [reply]},
       },
     },
      # this will be returned by the link search, and should be overriden by the
      # reply in self.activities[0]
      colliding_reply,
    ]
    FakeGrSource.search_results = links

    self.post_task()

    # expected responses:
    # * link
    # * link comment
    # * comment, like, and reshare from the normal activity
    expected = [
      Response(
        id='tag:source.com,2013:9',
        type='post',
        unsent=['http://target9/post/url'],
      ), Response(
        id='tag:source.com,2013:9_comment',
        type='comment',
        unsent=['http://target9/post/url'],
      )]

    # responses from the normal activity
    for resp in self.responses[:4]:
      resp.activities_json = [json_dumps({
        'id': 'tag:source.com,2013:a',
        'object': {'content': 'foo http://target1/post/url bar'},
      })]
    expected += self.responses[:4]

    self.assert_responses(expected, ignore=('activities_json', 'response_json',
                                            'source', 'original_posts'))

  def test_search_for_links_skips_posse_posts(self):
    """When mention search finds a POSSE post, it shouldn't backfeed it.

    https://github.com/snarfed/bridgy/issues/485
    """
    source = self.sources[0]
    source.domain_urls = ['http://foo.com/bar?biff']
    source.domains = ['or.ig']
    source.put()

    link = {
      'id': 'tag:or.ig,2013:9',
      'object': {'content': 'foo http://or.ig/post'},
    }
    FakeGrSource.search_results = [link]
    FakeGrSource.activities = []

    self.post_task()
    self.assert_responses([Response(
      id='tag:or.ig,2013:9',
      activities_json=[json_dumps(link)],
      response_json=json_dumps(link),
      type='post',
      source=source.key,
      status='complete',
      original_posts=['http://or.ig/post'],
    )])

  def test_search_for_links_skips_redirected_posse_post(self):
    """Same as above, with a redirect."""
    self.sources[0].domain_urls = ['http://foo']
    self.sources[0].domains = ['or.ig']
    self.sources[0].put()

    link = {
      'id': 'tag:or.ig,2013:9',
      'object': {'content': 'foo http://sho.rt/post'},
    }
    FakeGrSource.search_results = [link]
    FakeGrSource.activities = []

    self.expect_requests_head('http://sho.rt/post',
                              redirected_url='http://or.ig/post')
    self.mox.ReplayAll()
    self.post_task()

    self.assert_responses([Response(
      id='tag:or.ig,2013:9',
      activities_json=[json_dumps(link)],
      response_json=json_dumps(link),
      type='post',
      source=self.sources[0].key,
      status='complete',
      original_posts=['http://or.ig/post'],
    )])

  def test_user_mentions(self):
    """Search for and backfeed user mentions.

    A mention post itself should be backfed:
    https://github.com/snarfed/bridgy/issues/523

    ...but not a share (repost) of a mention:
    https://github.com/snarfed/bridgy/issues/819
    """
    source = self.sources[0]
    source.domain_urls = ['http://foo/', 'https://bar']
    source.put()

    obj = {
      'id': 'tag:source,2013:9',
      'tags': [{
        'objectType': 'person',
        'id': 'tag:source,2013:%s' % source.key.id(),
        'url': 'https://fa.ke/%s' % source.key.id(),
      }, {
        'objectType': 'person',
        'id': 'tag:source,2013:other',
        'url': 'https://fa.ke/other',
      }],
    }
    FakeGrSource.activities = [{
      'id': 'tag:source,2013:9',
      'verb': 'post',
      'object': obj,
    }, {
      'id': 'tag:source,2013:5',
      'verb': 'share',
      'object': obj,
    }]
    self.post_task()

    # one expected response with two target urls, one for each domain_url
    pruned = json_dumps({
      'id': 'tag:source,2013:9',
      'verb': 'post',
      'object': {'id': 'tag:source,2013:9'},
    })
    self.assert_responses([Response(
      id='tag:source,2013:9',
      activities_json=[json_dumps({'id': 'tag:source,2013:9'})],
      response_json=pruned,
      type='post',
      source=source.key,
      unsent=['http://foo/', 'https://bar'],
      original_posts=[],
    )])

  def test_post_has_both_link_and_user_mention(self):
    """https://github.com/snarfed/bridgy/issues/570"""
    source = self.sources[0]
    source.domain_urls = ['http://foo/']
    source.domains = ['foo']
    source.put()

    post = {
      'id': 'tag:source.com,2013:9',
      'object': {
        'author': {
          'name': 'bar',
          'id': 'tag:source:2013:bar',  # someone else
        },
        'content': 'http://foo/post @foo',
        'tags': [{
          'objectType': 'person',
          'id': 'tag:source,2013:%s' % source.key.id(),
        }],
      },
    }
    FakeGrSource.activities = [post]      # for user mention
    FakeGrSource.search_results = [post]  # for link search

    self.post_task()
    self.assert_responses([Response(
      id='tag:source.com,2013:9',
      type='post',
      unsent=['http://foo/post', 'http://foo/'],
    )], ignore=('activities_json', 'response_json', 'source', 'original_posts'))

  def test_post_attachment(self):
    """One silo post references another one; second should be propagated
    as a mention of the first.
    """
    source = self.sources[0]

    post = {
      'id': 'tag:source,2013:1234',
      'object': {
        'author': {
          'id': 'tag:source,2013:someone_else',
        },
        'content': 'That was a pretty great post',
        'attachments': [{
          'objectType': 'note',
          'content': 'This note is being referenced or otherwise quoted http://author/permalink',
          'author': {'id': source.user_tag_id()},
          'url': 'https://fa.ke/post/quoted',
        }]
      }
    }

    FakeGrSource.activities = [post]
    self.post_task()
    self.assert_responses([Response(
      id='tag:source,2013:1234',
      type='post',
      unsent=['http://author/permalink'],
    )], ignore=('activities_json', 'response_json', 'source', 'original_posts'))

  def test_post_attachment_and_user_mention(self):
    """One silo post references another one and also user mentions the
    other post's author. We should send webmentions for both references.
    """
    source = self.sources[0]

    post = {
      'id': 'tag:source,2013:1234',
      'object': {
        'author': {
          'id': 'tag:source,2013:someone_else',
        },
        'content': 'That was a pretty great post',
        'attachments': [{
          'objectType': 'note',
          'content': 'This note is being referenced or otherwise quoted http://author/permalink',
          'author': {'id': source.user_tag_id()},
          'url': 'https://fa.ke/post/quoted',
        }],
        'tags': [{
          'objectType': 'person',
          'id': source.user_tag_id(),
          'urls': [{'value': 'http://author'}],
        }],
      }
    }

    FakeGrSource.activities = [post]
    self.post_task()
    self.assert_responses([Response(
      id='tag:source,2013:1234',
      type='post',
      unsent=['http://author', 'http://author/permalink'],
    )], ignore=('activities_json', 'response_json', 'source', 'original_posts'))

  def test_wrong_last_polled(self):
    """If the source doesn't have our last polled value, we should quit.
    """
    self.sources[0].last_polled = datetime.datetime.utcfromtimestamp(3)
    self.sources[0].put()
    self.post_task()
    self.assertEqual([], list(Response.query()))

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
    self.assertEqual('enabled', self.sources[0].key.get().status)

  def test_disable_source_on_deauthorized(self):
    """If the source raises DisableSource, disable it.
    """
    source = self.sources[0]
    self.expect_get_activities().AndRaise(models.DisableSource)
    self.mox.ReplayAll()

    source.status = 'enabled'
    source.put()
    self.post_task()
    self.assertEqual('disabled', source.key.get().status)

  def test_site_specific_disable_sources(self):
    """HTTP 401 and 400 '' for Instagram should disable the source."""
    try:
      for err in (
          urllib.error.HTTPError('url', 401, 'msg', {}, io.StringIO('body')),
          urllib.error.HTTPError('url', 400, 'foo', {}, io.StringIO(
            '{"meta":{"error_type":"OAuthAccessTokenException"}}')),
          AccessTokenRefreshError('invalid_grant'),
          AccessTokenRefreshError('invalid_grant: Token has been revoked.'),
      ):
        self.mox.UnsetStubs()
        self.setUp()
        self.expect_get_activities().AndRaise(err)
        self.mox.ReplayAll()

        self.post_task()
        self.assertEqual('disabled', self.sources[0].key.get().status)

    finally:
      self.mox.UnsetStubs()

  def test_rate_limiting_errors(self):
    """Finish the task on rate limiting errors."""
    self.mox.stubs.Set(FakeSource, 'RATE_LIMIT_HTTP_CODES', ('429', '456'))
    self.mox.stubs.Set(self.sources[0], 'RATE_LIMIT_HTTP_CODES', ('429', '456'))
    try:
      error_body = json_dumps({"meta": {
        "code": 429, "error_message": "The maximum number of requests...",
        "error_type": "OAuthRateLimitException"}})
      for err in (
          urllib.error.HTTPError('url', 429, 'Rate limited', {},
                                 io.StringIO(error_body.decode('utf-8'))),
          apiclient.errors.HttpError(httplib2.Response({'status': 429}), b''),
      ):
        self.mox.UnsetStubs()
        self.expect_get_activities().AndRaise(err)
        self.mox.ReplayAll()

        self.post_task()
        source = self.sources[0].key.get()
        self.assertEqual('error', source.poll_status)
        self.assertTrue(source.rate_limited)
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
    FakeGrSource.etag = '"my etag"'
    self.post_task()

    source = self.sources[0].key.get()
    self.assertEqual('"my etag"', source.last_activities_etag)
    source.last_polled = util.EPOCH
    source.put()

    self.expect_get_activities(etag='"my etag"', min_id='c'
      ).AndReturn({'items': [], 'etag': '"new etag"'})
    self.mox.ReplayAll()
    self.post_task()

    source = self.sources[0].key.get()
    self.assertEqual('"new etag"', source.last_activities_etag)

  def test_last_activity_id(self):
    """We should store the last activity id seen and then send it as min_id."""
    FakeGrSource.activities = list(reversed(self.activities))
    self.post_task()

    source = self.sources[0].key.get()
    self.assertEqual('c', source.last_activity_id)
    source.last_polled = util.EPOCH
    source.put()

    self.expect_get_activities(min_id='c').AndReturn({'items': []})
    self.mox.ReplayAll()
    self.post_task()

  def test_last_activity_id_not_tag_uri(self):
    self.activities[0]['id'] = 'a'
    self.activities[1]['id'] = 'b'
    self.activities[2]['id'] = 'c'
    FakeGrSource.activities = list(reversed(self.activities))
    self.post_task()
    self.assertEqual('c', self.sources[0].key.get().last_activity_id)

  def test_cache_trims_to_returned_activity_ids(self):
    """We should trim last_activities_cache_json to just the returned activity ids."""
    source = self.sources[0]
    source.last_activities_cache_json = json_dumps(
      {1: 2, 'x': 'y', 'prefix x': 1, 'prefix b': 0})
    source.put()

    self.post_task()

    self.assert_equals({'prefix b': 0},
                       json_loads(source.key.get().last_activities_cache_json))

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

  def _expect_fetch_hfeed(self):
    self.expect_requests_get('http://author', """
    <html class="h-feed">
      <a class="h-entry" href="/permalink"></a>
      <div class="h-entry">
        <a class="u-url" href="http://author/permalink"></a>
        <a class="u-syndication" href="http://fa.ke/post/url"></a>
      </div>
    </html>""")

  def test_set_last_syndication_url(self):
    """A successful posse-post-discovery round should set
    last_syndication_url to approximately the current time.
    """
    self.sources[0].domain_urls = ['http://author']
    FakeGrSource.DOMAIN = 'source'
    self.sources[0].last_syndication_url = None
    self.sources[0].put()

    # leave at least one new response to trigger PPD
    for r in self.responses[:-1]:
      r.status = 'complete'
      r.put()

    self._expect_fetch_hfeed()
    self.mox.ReplayAll()
    self.post_task()

    # query source
    source = self.sources[0].key.get()
    self.assertEquals(NOW, source.last_syndication_url)

  def test_multiple_activities_fetch_hfeed_once(self):
    """Make sure that multiple activities only fetch the author's h-feed once.
    """
    self.sources[0].domain_urls = ['http://author']
    self.sources[0].put()

    FakeGrSource.activities = self.activities

    # syndicated urls need to be unique for this to be interesting
    for letter, activity in zip(string.letters, FakeGrSource.activities):
      activity['url'] = activity['object']['url'] = 'http://fa.ke/post/' + letter
      activity['object']['content'] = 'foo bar'

    self._expect_fetch_hfeed()
    self.mox.ReplayAll()
    self.post_task()

  def test_syndicated_post_does_not_prevent_fetch_hfeed(self):
    """The original fix to fetch the source's h-feed only once per task
    had a bug that prevented us from fetching the h-feed *at all* if
    there was already a SyndicatedPost for the first activity.

    https://github.com/snarfed/bridgy/issues/597#issuecomment-214079860
    """
    self.sources[0].domain_urls = ['http://author']
    self.sources[0].put()

    FakeGrSource.activities = self.activities

    # syndicated urls need to be unique for this to be interesting
    for letter, activity in zip(string.letters, FakeGrSource.activities):
      activity['url'] = activity['object']['url'] = 'http://fa.ke/post/' + letter
      activity['object']['content'] = 'foo bar'

    # set up a blank, which will short-circuit fetch for the first activity
    SyndicatedPost.insert_syndication_blank(
      self.sources[0],
      self.sources[0].canonicalize_url(self.activities[0].get('url')))

    self._expect_fetch_hfeed()
    self.mox.ReplayAll()
    self.post_task()

  def _setup_refetch_hfeed(self):
    self.sources[0].domain_urls = ['http://author']
    ten_min = datetime.timedelta(minutes=10)
    self.sources[0].last_syndication_url = NOW - ten_min
    self.sources[0].last_hfeed_refetch = NOW - models.Source.FAST_REFETCH - ten_min
    self.sources[0].put()

    # pretend we've already done posse-post-discovery for the source
    # and checked this permalink and found no back-links
    SyndicatedPost(parent=self.sources[0].key, original=None,
                   syndication='https://fa.ke/post/url').put()
    SyndicatedPost(parent=self.sources[0].key,
                   original='http://author/permalink',
                   syndication=None).put()

    # and all the status have already been sent
    for r in self.responses:
      r.status = 'complete'
      r.put()

  def test_do_not_refetch_hfeed(self):
    """Only 1 hour has passed since we last re-fetched the user's h-feed. Make
    sure it is not fetched again."""
    self._setup_refetch_hfeed()
    # too recent to fetch again
    self.sources[0].last_hfeed_refetch = hour_ago = NOW - datetime.timedelta(hours=1)
    self.sources[0].put()

    self.mox.ReplayAll()
    self.post_task()
    self.assertEquals(hour_ago, self.sources[0].key.get().last_hfeed_refetch)

    # should still be a blank SyndicatedPost
    relationships = SyndicatedPost.query(
      SyndicatedPost.original == 'http://author/permalink',
      ancestor=self.sources[0].key).fetch()
    self.assertEqual(1, len(relationships))
    self.assertIsNone(relationships[0].syndication)

    # should not repropagate any responses
    self.assertEquals(0, len(self.taskqueue_stub.GetTasks('propagate')))

  def test_dont_repropagate_posses(self):
    """If we find a syndication URL for a POSSE post, we shouldn't repropagate it.
    """
    self.sources[0].domain_urls = ['http://author']
    self.sources[0].last_syndication_url = NOW - datetime.timedelta(minutes=10)
    FakeGrSource.activities = []
    self.sources[0].put()

    # the one existing response is a POSSE of that post
    resp = Response(
      id='tag:or.ig,2013:9',
      response_json='{}',
      activities_json=['{"url": "http://fa.ke/post/url"}'],
      source=self.sources[0].key,
      status='complete',
      original_posts=['http://author/permalink'],
    )
    resp.put()
    self.responses = [resp]

    self._expect_fetch_hfeed()
    self.mox.ReplayAll()
    self.post_task()

    # shouldn't repropagate it
    self.assertEquals(0, len(self.taskqueue_stub.GetTasks('propagate')))
    self.assertEquals('complete', resp.key.get().status)

  def test_do_refetch_hfeed(self):
    """Emulate a situation where we've done posse-post-discovery earlier and
    found no rel=syndication relationships for a particular silo URL. Every
    two hours or so, we should refetch the author's page and check to see if
    any new syndication links have been added or updated.
    """
    self._setup_refetch_hfeed()
    self._expect_fetch_hfeed()
    self.mox.ReplayAll()
    self.post_task()

    # should have a new SyndicatedPost
    relationships = SyndicatedPost.query(
      SyndicatedPost.original == 'http://author/permalink',
      ancestor=self.sources[0].key).fetch()
    self.assertEquals(1, len(relationships))
    self.assertEquals('https://fa.ke/post/url', relationships[0].syndication)

    # should repropagate all 12 responses
    tasks = self.taskqueue_stub.GetTasks('propagate')
    self.assertEquals(12, len(tasks))

    # and they should be in reverse creation order
    response_keys = [resp.key.urlsafe() for resp in self.responses]
    response_keys.reverse()
    task_keys = [testutil.get_task_params(task)['response_key']
                 for task in tasks]
    self.assertEquals(response_keys, task_keys)

    source = self.sources[0].key.get()
    self.assertEquals(NOW, source.last_syndication_url)
    self.assertEquals(NOW, source.last_hfeed_refetch)

  def test_refetch_hfeed_trigger(self):
    self.sources[0].domain_urls = ['http://author']
    FakeGrSource.DOMAIN = 'source'
    self.sources[0].last_syndication_url = None
    self.sources[0].last_hfeed_refetch = models.REFETCH_HFEED_TRIGGER
    self.sources[0].put()

    FakeGrSource.activities = []

    self._expect_fetch_hfeed()
    self.mox.ReplayAll()
    self.post_task()

  def test_refetch_hfeed_repropagate_responses_query_expired(self):
    """https://github.com/snarfed/bridgy/issues/515"""
    class BadRequestError(BaseException):
      pass

    self._test_refetch_hfeed_repropagate_responses_exception(
      BadRequestError('The requested query has expired. Please restart it with the last cursor to read more results.'))

  def test_refetch_hfeed_repropagate_responses_timeout(self):
    """https://github.com/snarfed/bridgy/issues/514"""
    class Timeout(BaseException):
      pass

    self._test_refetch_hfeed_repropagate_responses_exception(
      Timeout('The datastore operation timed out, or the data was temporarily unavailable.'))

  def test_refetch_hfeed_repropagate_responses_http_exception_deadline(self):
    self._test_refetch_hfeed_repropagate_responses_exception(
      http.client.HTTPException('Deadline exceeded foo bar'))

  def _test_refetch_hfeed_repropagate_responses_exception(self, exception):
    self._setup_refetch_hfeed()
    self._expect_fetch_hfeed()

    self.mox.StubOutWithMock(Response, 'query')
    Response.query(Response.source == self.sources[0].key).AndRaise(exception)
    self.mox.ReplayAll()

    # should 200
    self.post_task()
    self.assertEquals(NOW, self.sources[0].key.get().last_hfeed_refetch)

  def test_response_changed(self):
    """If a response changes, we should repropagate it from scratch.
    """
    source = self.sources[0]
    activity = self.activities[0]

    # just one response: self.responses[0]
    tags = activity['object']['tags']
    del activity['object']['tags']
    FakeGrSource.activities = [activity]

    # first change to response
    self._change_response_and_poll()

    # second change to response
    self._change_response_and_poll()

    # return new response *and* existing response. both should be stored in
    # Source.seen_responses_cache_json
    replies = activity['object']['replies']['items']
    replies.append(self.activities[1]['object']['replies']['items'][0])

    self.post_task(reset=True)
    self.assert_equals(replies, json_loads(source.key.get().seen_responses_cache_json))
    self.responses[4].key.delete()

    # new responses that don't include existing response. cache will have
    # existing response.
    del activity['object']['replies']
    activity['object']['tags'] = tags

    self.post_task(reset=True)
    self.assert_equals([r.key for r in self.responses[:4]],
                       list(Response.query().iter(keys_only=True)))
    self.assert_equals(tags, json_loads(source.key.get().seen_responses_cache_json))

  def _change_response_and_poll(self):
    resp = self.responses[0].key.get() or self.responses[0]
    old_resp_jsons = resp.old_response_jsons + [resp.response_json]
    targets = resp.sent = resp.unsent
    resp.unsent = []
    resp.status = 'complete'
    resp.put()

    reply = self.activities[0]['object']['replies']['items'][0]
    reply['content'] += ' xyz'
    new_resp_json = json_dumps(reply)
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
    self.assert_equals([reply], json_loads(source.seen_responses_cache_json))

  def test_in_blocklist(self):
    """Responses from blocked users should be ignored."""
    self.mox.StubOutWithMock(FakeSource, 'is_blocked')
    FakeSource.is_blocked(mox.IgnoreArg()).AndReturn(False)
    FakeSource.is_blocked(mox.IgnoreArg()).AndReturn(True)  # block second response
    FakeSource.is_blocked(mox.IgnoreArg()).MultipleTimes(10).AndReturn(False)
    self.mox.ReplayAll()

    self.post_task()
    self.assertEqual(11, Response.query().count())
    expected = [self.responses[0]] + self.responses[2:]
    self.assert_responses(expected)

    tasks = self.taskqueue_stub.GetTasks('propagate')
    keys = [ndb.Key(urlsafe=testutil.get_task_params(t)['response_key'])
            for t in tasks]
    self.assert_equals(keys, [r.key for r in expected])


class DiscoverTest(TaskQueueTest):

  post_url = '/_ah/queue/discover'

  def setUp(self):
    super(DiscoverTest, self).setUp()
    appengine_config.DEBUG = True

  def tearDown(self):
    appengine_config.DEBUG = False
    super(DiscoverTest, self).tearDown()

  def discover(self, **kwargs):
    super(DiscoverTest, self).post_task(params={
      'source_key': self.sources[0].key.urlsafe(),
      'post_id': 'b',
    }, **kwargs)

  def assert_propagating(self, responses):
    """Asserts that all of the responses have propagate tasks."""
    tasks = self.taskqueue_stub.GetTasks('propagate')
    for task in tasks:
      self.assertEqual('/_ah/queue/propagate', task['url'])
    keys = [ndb.Key(urlsafe=testutil.get_task_params(t)['response_key'])
            for t in tasks]
    self.assert_equals([r.key for r in responses], keys)

  def test_new(self):
    """A new silo post we haven't seen before."""
    self.mox.StubOutWithMock(FakeSource, 'get_activities')
    FakeSource.get_activities(
      activity_id='b', fetch_replies=True, fetch_likes=True, fetch_shares=True,
      user_id=self.sources[0].key.id()).AndReturn([self.activities[1]])
    self.mox.ReplayAll()

    self.assertEqual(0, Response.query().count())
    self.discover()
    self.assert_responses(self.responses[4:8])
    self.assert_propagating(self.responses[4:8])

  def test_no_post(self):
    """Silo post not found."""
    FakeGrSource.activities = []
    self.discover()
    self.assert_responses([])
    self.assert_propagating([])

  def test_restart_existing_tasks(self):
    FakeGrSource.activities = [self.activities[1]]

    resps = self.responses[4:8]
    resps[0].status = 'new'
    resps[1].status = 'processing'
    resps[2].status = 'complete'
    resps[3].status = 'error'
    resps[0].sent = resps[1].error = resps[2].failed = resps[3].skipped = \
        ['http://target/2']
    for resp in resps:
      resp.put()

    self.discover()

    for resp in Response.query():
      self.assert_equals('new', resp.status)
      self.assert_equals(['http://target1/post/url', 'http://target/2'],
                         resp.unsent, resp.key)
    self.assert_propagating(resps)

  def test_reply(self):
    """If the activity is a reply, we should also enqueue the in-reply-to post."""
    self.mox.StubOutWithMock(FakeSource, 'get_activities')
    FakeSource.get_activities(
      activity_id='b', fetch_replies=True, fetch_likes=True, fetch_shares=True,
      user_id=self.sources[0].key.id()).AndReturn([{
        'id': 'tag:fake.com:123',
        'object': {
          'id': 'tag:fake.com:123',
          'url': 'https://twitter.com/_/status/123',
          'inReplyTo': [{'id': 'tag:fake.com:456'}],
        },
      }])
    self.mox.ReplayAll()

    self.discover()
    tasks = self.taskqueue_stub.GetTasks('discover')
    self.assertEqual(1, len(tasks))
    self.assertEqual('/_ah/queue/discover', tasks[0]['url'])
    self.assertEqual({
      'source_key': self.sources[0].key.urlsafe(),
      'post_id': '456',
    }, testutil.get_task_params(tasks[0]))

  def test_get_activities_error(self):
    self._test_get_activities_error(400)

  def test_get_activities_rate_limited(self):
    self._test_get_activities_error(429)

  def _test_get_activities_error(self, status):
    self.expect_get_activities(activity_id='b', user_id=self.sources[0].key.id()
        ).AndRaise(urllib.error.HTTPError('url', status, 'Rate limited', {}, None))
    self.mox.ReplayAll()

    self.discover(expected_status=ERROR_HTTP_RETURN_CODE)
    self.assert_responses([])
    self.assert_propagating([])

  def test_event_type(self):
    self.mox.StubOutWithMock(FakeGrSource, 'get_event')
    FakeGrSource.get_event('321').AndReturn(self.activities[0])
    self.mox.ReplayAll()

    self.post_task(params={
      'source_key': self.sources[0].key.urlsafe(),
      'post_id': '321',
      'type': 'event',
    })
    self.assert_responses(self.responses[:4])
    self.assert_propagating(self.responses[:4])


class PropagateTest(TaskQueueTest):

  post_url = '/_ah/queue/propagate'

  def setUp(self):
    super(PropagateTest, self).setUp()
    for r in self.responses[:4]:
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
                        error=None, input_endpoint=None, discovered_endpoint=None,
                        headers=util.REQUEST_HEADERS):
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
    return mock_send.send(timeout=999, headers=headers)

  def test_propagate(self):
    """Normal propagate tasks."""
    self.assertEqual('new', self.responses[0].status)

    id = self.sources[0].key.string_id()
    for url in (
        'http://localhost/comment/fake/%s/a/1_2_a' % id,
        'http://localhost/like/fake/%s/a/alice' % id,
        'http://localhost/repost/fake/%s/a/bob' % id,
        'http://localhost/react/fake/%s/a/bob/a_scissors_by_bob' % id,
    ):
      self.expect_webmention(source_url=url).AndReturn(True)
    self.mox.ReplayAll()

    now = NOW
    util.now_fn = lambda: now

    for r in self.responses[:4]:
      now += datetime.timedelta(hours=1)
      self.post_task(response=r)
      self.assert_response_is('complete', now + LEASE_LENGTH,
                              sent=['http://target1/post/url'], response=r)
      self.assert_equals(now, self.sources[0].key.get().last_webmention_sent)
      util.webmention_endpoint_cache.clear()

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

  def test_errors_and_caching_endpoint(self):
    """BAD_TARGET_URL shouldn't cache anything. RECEIVER_ERROR should cache
    endpoint, not error."""
    self.expect_webmention(error={'code': 'BAD_TARGET_URL'}).AndReturn(False)
    # shouldn't have a cached endpoint
    self.expect_webmention(error={'code': 'RECEIVER_ERROR'}).AndReturn(False)
    # should have and use a cached endpoint
    self.expect_webmention(input_endpoint='http://webmention/endpoint'
                           ).AndReturn(True)
    self.mox.ReplayAll()

    self.post_task(expected_status=ERROR_HTTP_RETURN_CODE)
    self.assert_response_is('error', error=['http://target1/post/url'])

    self.responses[0].status = 'new'
    self.responses[0].put()
    self.post_task(expected_status=ERROR_HTTP_RETURN_CODE)
    self.assert_response_is('error', error=['http://target1/post/url'])

    self.responses[0].status = 'new'
    self.responses[0].put()
    self.post_task()
    self.assert_response_is('complete', sent=['http://target1/post/url'])

  def test_cached_webmention_discovery_shouldnt_refresh_cache(self):
    """A cached webmention discovery shouldn't be written back to the cache."""
    # first wm discovers and finds no endpoint, second uses cache, third rediscovers
    self.expect_webmention(error={'code': 'NO_ENDPOINT'}).AndReturn(False)
    self.expect_webmention().AndReturn(True)
    self.mox.ReplayAll()

    # inject a fake time.time into the cache
    now = time.time()
    util.webmention_endpoint_cache = TTLCache(500, 2, timer=lambda: now)

    self.post_task()
    self.assert_response_is('complete', skipped=['http://target1/post/url'])

    now += 1
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
    self.expect_webmention_requests_get(
      'http://not/html', content_type='image/gif', timeout=999)

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
    self.expect_webmention_requests_get(
      'http://html/charset',
      content_type='text/html; charset=utf-8',
      response_headers={'Link': '<http://my/endpoint>; rel="webmention"'},
      timeout=999)

    source_url = ('http://localhost/comment/fake/%s/a/1_2_a' %
                  self.sources[0].key.string_id())
    self.expect_requests_post(
      'http://my/endpoint',
      data={'source': source_url, 'target': 'http://html/charset'},
      stream=None, timeout=999, verify=False, allow_redirects=False, headers={'Accept': '*/*'})

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
    self.expect_webmention_requests_get('http://unknown/type', content_type=None,
                                        timeout=999)

    self.mox.ReplayAll()
    self.post_task()
    self.assert_response_is('complete', skipped=['http://unknown/type'])

  def test_link_header_rel_webmention_unquoted(self):
    """We should support rel=webmention (no quotes) in the Link header."""
    self.mox.UnsetStubs()  # drop WebmentionSend mock; let it run
    super(PropagateTest, self).setUp()

    self.responses[0].unsent = ['http://my/post']
    self.responses[0].put()
    self.expect_requests_head('http://my/post')
    self.expect_webmention_requests_get(
      'http://my/post', timeout=999,
      response_headers={'Link': '<http://my/endpoint>; rel=webmention'})

    source_url = ('http://localhost/comment/fake/%s/a/1_2_a' %
                  self.sources[0].key.string_id())
    self.expect_requests_post(
      'http://my/endpoint', timeout=999, verify=False,
      data={'source': source_url, 'target': 'http://my/post'},
      stream=None, allow_redirects=False, headers={'Accept': '*/*'})

    self.mox.ReplayAll()
    self.post_task()
    self.assert_response_is('complete', sent=['http://my/post'])

  def test_webmention_post_omits_accept_header(self):
    """The webmention POST request should never send the Accept header."""
    self.mox.UnsetStubs()  # drop WebmentionSend mock; let it run
    super(PropagateTest, self).setUp()

    self.responses[0].source = Twitter(id='rhiaro').put()
    self.responses[0].put()
    # self.expect_requests_head('http://my/post')
    self.expect_webmention_requests_get(
      'http://target1/post/url', timeout=999,
      headers=util.REQUEST_HEADERS_CONNEG,
      response_headers={'Link': '<http://my/endpoint>; rel=webmention'})

    self.expect_requests_post(
      'http://my/endpoint', timeout=999, verify=False,
      data={'source': 'http://localhost/comment/twitter/rhiaro/a/1_2_a',
            'target': 'http://target1/post/url'},
      stream=None, allow_redirects=False, headers={'Accept': '*/*'})

    self.mox.ReplayAll()
    self.post_task()
    self.assert_response_is('complete', sent=['http://target1/post/url'])

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
    url = 'https://maps/?q=' + urllib.parse.quote_plus('3 Cours de la République'.encode('utf-8'))
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
    activity = json_loads(self.responses[0].activities_json[0])
    activity['to'] = [{'objectType':'group', 'alias':'@private'}]
    self.responses[0].activities_json = [json_dumps(activity)]
    self.responses[0].put()

    self.post_task()
    self.assert_response_is('complete', unsent=['http://target1/post/url'], sent=[])

  def test_non_public_response(self):
    """If the response is non-public, we should give up."""
    resp = json_loads(self.responses[0].response_json)
    resp['to'] = [{'objectType':'group', 'alias':'@private'}]
    self.responses[0].response_json = json_dumps(resp)
    self.responses[0].put()

    self.post_task()
    self.assert_response_is('complete', unsent=['http://target1/post/url'], sent=[])

  def test_webmention_fail(self):
    """If sending the webmention fails, the lease should be released."""
    for error, status, bucket in (
        ({'code': 'NO_ENDPOINT'}, 'complete', 'skipped'),
        ({'code': 'BAD_TARGET_URL'}, 'error', 'error'),
        ({'code': 'RECEIVER_ERROR', 'http_status': 400}, 'complete', 'failed'),
        ({'code': 400, 'http_status': 400}, 'complete', 'failed'),
        ({'code': 'RECEIVER_ERROR', 'http_status': 500}, 'error', 'error')
      ):
      self.mox.UnsetStubs()
      self.setUp()
      self.responses[0].status = 'new'
      self.responses[0].put()
      self.expect_webmention(error=error).AndReturn(False)
      self.mox.ReplayAll()

      logging.debug('Testing %s', error)
      expected_status = ERROR_HTTP_RETURN_CODE if bucket == 'error' else 200
      self.post_task(expected_status=expected_status)
      self.assert_response_is(status, **{bucket: ['http://target1/post/url']})
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
    self.post_task(base_url='https://brid.gy')

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
    self.post_task(base_url='https://brid-gy.appspot.com')

  def test_activity_id_not_tag_uri(self):
    """If the activity id isn't a tag uri, we should just use it verbatim."""
    activity = json_loads(self.responses[0].activities_json[0])
    activity['id'] = 'AAA'
    self.responses[0].activities_json = [json_dumps(activity)]

    self.responses[0].unsent = ['http://good']
    self.responses[0].put()

    source_url = 'https://brid-gy.appspot.com/comment/fake/%s/AAA/1_2_a' % \
        self.sources[0].key.string_id()
    self.expect_webmention(source_url=source_url, target='http://good')\
        .AndReturn(True)

    self.mox.ReplayAll()
    self.post_task(base_url='https://brid.gy')

  def test_response_with_multiple_activities(self):
    """Should use Response.urls_to_activity to generate the source URLs.
    """
    self.responses[0].activities_json = [
      '{"id": "000"}', '{"id": "111"}', '{"id": "222"}']
    self.responses[0].unsent = ['http://AAA', 'http://BBB', 'http://CCC']
    self.responses[0].urls_to_activity = json_dumps(
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
    self.post_task(base_url='https://brid.gy')

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
    self.responses[0].urls_to_activity = json_dumps({'bad': 9})
    self.responses[0].put()
    self.mox.ReplayAll()
    self.post_task(expected_status=ERROR_HTTP_RETURN_CODE)

  def test_source_url_index_error(self):
    """We should gracefully retry when we hit the IndexError bug.

    https://github.com/snarfed/bridgy/issues/237
    """
    self.responses[0].activities_json = []
    self.responses[0].put()
    self.mox.ReplayAll()
    self.post_task(expected_status=ERROR_HTTP_RETURN_CODE)

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
                               unsent=['https://brid.gy/publish/facebook'])
    blogpost.put()

    self.expect_requests_head('https://brid.gy/publish/facebook')
    self.expect_webmention(
      source_url='x',
      target='https://brid.gy/publish/facebook',
      discovered_endpoint='https://brid.gy/publish/webmention',
      ).AndReturn(True)
    self.mox.ReplayAll()

    self.post_url = '/_ah/queue/propagate-blogpost'
    super(PropagateTest, self).post_task(params={'key': blogpost.key.urlsafe()})
    self.assert_response_is('complete', response=blogpost,
                            sent=['https://brid.gy/publish/facebook'])

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

  def test_post_response(self):
    """Responses with type 'post' (ie mentions) are their own activity.

    https://github.com/snarfed/bridgy/issues/456
    """
    self.responses[0].type = 'post'
    self.responses[0].response_json = json_dumps(json_loads(
      self.responses[0].activities_json[0]))
    self.responses[0].put()

    self.expect_webmention(source_url='http://localhost/post/fake/0123456789/a'
                          ).AndReturn(True)
    self.mox.ReplayAll()
    self.post_task()
