"""Unit tests for tasks.py."""
import copy
import datetime
import http.client
import socket
import string
import io
import time
from unittest import skip
import urllib.request, urllib.parse, urllib.error

from cachetools import TTLCache
from google.cloud import ndb
from google.cloud.ndb._datastore_types import _MAX_STRING_LENGTH
from google.cloud.tasks_v2.types import Task
from mox3 import mox
from oauth_dropins.webutil.appengine_config import tasks_client
from oauth_dropins.webutil.testutil import NOW
from oauth_dropins.webutil.util import json_dumps, json_loads
import requests

import models
from models import Response, SyndicatedPost
from twitter import Twitter
import tasks
from . import testutil
from .testutil import FakeSource, FakeGrSource
import util
from util import ERROR_HTTP_RETURN_CODE, POLL_TASK_DATETIME_FORMAT

LEASE_LENGTH = tasks.SendWebmentions.LEASE_LENGTH


class TaskTest(testutil.BackgroundTest):
  """Attributes:
      post_url: the URL for post_task() to post to
  """
  post_url = None

  def setUp(self):
    super().setUp()
    self.sources[0].put()

  def post_task(self, expected_status=200, params={}, **kwargs):
    """Args:
      expected_status: integer, the expected HTTP return code
    """
    resp = self.client.post(self.post_url, data=params, **kwargs)
    self.assertEqual(expected_status, resp.status_code)

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
      if resp.urls_to_activity and 'urls_to_activity' not in ignore:
        resp.urls_to_activity = json_dumps(json_loads(resp.urls_to_activity), sort_keys=True)
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


class PollTest(TaskTest):

  post_url = '/_ah/queue/poll'

  def setUp(self):
    super().setUp()
    FakeGrSource.DOMAIN = 'source'

    self.quote_post = {
      'id': 'tag:source,2013:1234',
      'object': {
        'author': {
          'id': 'tag:source,2013:someone_else',
        },
        'content': 'That was a pretty great post',
        'attachments': [{
          'objectType': 'note',
          'content': 'This note is being referenced or otherwise quoted http://author/permalink',
          'author': {'id': self.sources[0].user_tag_id()},
          'url': 'https://fa.ke/post/quoted',
        }]
      }
    }

  def tearDown(self):
    FakeGrSource.DOMAIN = 'fa.ke'
    super().tearDown()

  def post_task(self, expected_status=200, source=None, reset=False,
                expect_poll=None, expect_last_polled=None):
    if expect_poll:
      last_polled = (expect_last_polled or util.now()).strftime(
        POLL_TASK_DATETIME_FORMAT)
      self.expect_task('poll', eta_seconds=expect_poll.total_seconds(),
                       source_key=self.sources[0], last_polled=last_polled)
      self.mox.ReplayAll()

    if source is None:
      source = self.sources[0]

    if reset:
      source = source.key.get()
      source.last_polled = util.EPOCH
      source.put()

    super().post_task(expected_status=expected_status, params={
        'source_key': source.key.urlsafe().decode(),
        'last_polled': '1970-01-01-00-00-00',
      })

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
    return super().expect_get_activities(**full_kwargs)

  def test_poll(self):
    """A normal poll task."""
    self.assertEqual(0, Response.query().count())

    for resp in self.responses:
      self.expect_task('propagate', response_key=resp)

    self.post_task(expect_poll=FakeSource.FAST_POLL)
    self.assertEqual(12, Response.query().count())
    self.assert_responses()

    source = self.sources[0].key.get()
    self.assertEqual(NOW, source.last_polled)
    self.assertEqual('ok', source.poll_status)

  def test_poll_no_auto_poll(self):
    FakeGrSource.clear()
    self.stub_create_task()
    self.mox.stubs.Set(FakeSource, 'AUTO_POLL', False)
    self.mox.ReplayAll()
    self.post_task()

  def test_poll_status_polling(self):
    def check_poll_status(*args, **kwargs):
      self.assertEqual('polling', self.sources[0].key.get().poll_status)

    self.expect_get_activities().WithSideEffects(check_poll_status) \
                                .AndReturn({'items': []})
    self.mox.ReplayAll()
    self.post_task()
    self.assertEqual('ok', self.sources[0].key.get().poll_status)

  def test_poll_error(self):
    """If anything goes wrong, the poll status should be set to 'error'."""
    self.expect_get_activities().AndRaise(Exception('foo'))
    self.mox.ReplayAll()

    self.assertRaises(Exception, self.post_task, expect_poll=False)
    self.assertEqual('error', self.sources[0].key.get().poll_status)

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
    expected = [f'http://tar.get/{i}' for i in ('a', 'b', 'c', 'd', 'e', 'f')]
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
    self.activities[0]['object'].update({
      'tags': [],
      'content': 'http://fails/resolve',
    })
    FakeGrSource.activities = [self.activities[0]]
    self.expect_requests_head('http://fails/resolve', status_code=400)

    self.mox.ReplayAll()
    self.post_task()
    self.assert_equals(['http://fails/resolve'],
                       self.responses[0].key.get().unsent)

  def test_invalid_and_blocklisted_urls(self):
    """Target URLs with domains in the blocklist should be ignored.

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

    self.assertEqual(1, Response.query().count())
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
    now = NOW.replace(microsecond=0)
    self.activities[1]['object']['published'] = now.isoformat()

    self.activities[2]['object']['to'] = [{'objectType':'group', 'alias':'@public'}]
    public_date = now - datetime.timedelta(weeks=1)
    self.activities[2]['object']['published'] = public_date.isoformat()

    # Facebook returns 'unknown' for wall posts
    unknown = copy.deepcopy(self.activities[1])
    unknown['id'] = unknown['object']['id'] = 'x'
    unknown['object']['to'] = [{'objectType': 'unknown'}]
    self.activities.append(unknown)

    for resp in self.responses[:4] + self.responses[8:]:
      self.expect_task('propagate', response_key=resp)

    self.post_task(expect_poll=FakeSource.FAST_POLL)

    source = self.sources[0].key.get()
    self.assertEqual(public_date, source.last_public_post)
    self.assertEqual(2, source.recent_private_posts)

  def test_non_public_responses(self,):
    self.activities = FakeGrSource.activities = [self.activities[0]]

    unlisted = [{'objectType':'group', 'alias':'@unlisted'}]
    public = [{'objectType':'group', 'alias':'@public'}]

    self.activities[0]['object']['replies']['items'][0]['author'] = {'to': unlisted}
    self.activities[0]['object']['tags'][0]['actor'] = {'to': unlisted}
    self.activities[0]['object']['tags'][1]['to'] = unlisted
    self.activities[0]['object']['tags'][2]['to'] = public

    self.expect_task('propagate', response_key=self.responses[3])
    self.post_task(expect_poll=FakeSource.FAST_POLL)

    source = self.sources[0].key.get()
    self.assertEqual(0, source.recent_private_posts)

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

    resp_key = ndb.Key(Response, 'tag:source.com,2013:only_reply')
    self.expect_task('propagate', response_key=resp_key)

    self.post_task(expect_poll=FakeSource.FAST_POLL)
    self.assertEqual(1, Response.query().count())
    resp = Response.query().get()
    self.assert_equals([f'tag:source.com,2013:{id}' for id in ('a', 'b', 'c')],
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

    https://github.com/snarfed/bridgy/issues/51
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
         'author': {'id': source.user_tag_id()},
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

    self.assert_responses(expected, ignore=('activities_json', 'urls_to_activity',
                                            'response_json', 'source', 'original_posts'))

  def test_other_peoples_links_dont_backfeed_comments(self):
    """Don't backfeed comments on links from other people (ie not the user).

    https://github.com/snarfed/bridgy/issues/51
    https://github.com/snarfed/bridgy/issues/456
    """
    source = self.sources[0]
    source.domains = ['target']
    source.put()

    FakeGrSource.activities = []
    FakeGrSource.search_results = [{
       'id': 'tag:source.com,2013:777',
       'object': {
         'objectType': 'note',
         'content': 'foo http://target/777 bar',
         'replies': {'items': [{
           'objectType': 'comment',
           'id': 'tag:source.com,2013:777_comment',
           'content': 'baz biff',
         }]},
         'author': {'id': 'tag:fa.ke,2013:not_the_user'},
       },
     },
    ]

    self.post_task()

    # only the link post should be backfed, not the comment
    self.assert_responses([Response(
      id='tag:source.com,2013:777',
      type='post',
      unsent=['http://target/777'],
    )], ignore=('activities_json', 'urls_to_activity', 'response_json', 'source',
                'original_posts'))

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
        'id': f'tag:source,2013:{source.key.id()}',
        'url': f'https://fa.ke/{source.key.id()}',
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
      urls_to_activity='{"http://foo/":0,"https://bar":0}',
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
          'objectType': 'mention',
          'id': f'tag:source,2013:{source.key.id()}',
          'url': 'http://foo/',
        }],
      },
    }
    FakeGrSource.activities = [post]      # for user mention
    FakeGrSource.search_results = [post]  # for link search

    self.post_task()
    self.assert_responses([Response(
      id='tag:source.com,2013:9',
      type='post',
      unsent=['http://foo/', 'http://foo/post'],
      activities_json=[json_dumps({
        'id': 'tag:source.com,2013:9',
        'object': {'content': 'http://foo/post @foo'},
      })],
      urls_to_activity='{"http://foo/post":0,"http://foo/":0}',
    )], ignore=('response_json', 'source', 'original_posts'))

  def test_comment_has_user_mention(self):
    """If a comment also @-memtions the OP's author (common on Mastodon), use the reply.

    https://github.com/snarfed/bridgy/issues/533
    """
    source = self.sources[0]
    post = self.activities[0]
    del post['object']['tags']
    self.activities = [post]

    comment = self.activities[0]['object']['replies']['items'][0]
    comment['tags'] = [{
      'objectType': 'mention',
      'id': 'tag:source,2013:should-not-use',
      'url': 'http://foo/',
    }]
    FakeGrSource.activities = [post, comment]

    self.post_task()
    self.assert_responses([Response(
      id='tag:source.com,2013:1_2_a',
      type='comment',
      unsent=['http://target1/post/url'],
      activities_json=[json_dumps({
        'id':'tag:source.com,2013:a',
        'url':'http://fa.ke/post/url',
        'object':{'content':'foo http://target1/post/url bar'},
      })],
      urls_to_activity='{"http://target1/post/url":0}',
    )], ignore=('response_json', 'source', 'original_posts'))

  def test_first_find_as_user_mention_then_as_comment(self):
    """If we find an @-mention, and later find it as a reply, we should add the OP activity.

    https://github.com/snarfed/bridgy/issues/533
    """
    mention_activity = json_dumps({'id':'tag:source.com,2013:1_2_a'})
    resp = Response(
      id='tag:source.com,2013:1_2_a',
      type='post',
      activities_json=[mention_activity],
      response_json=json_dumps({
        'objectType': 'comment',
        'id': 'tag:source.com,2013:1_2_a',
        'url': 'http://fa.ke/comment/url',
      }),
    )
    resp.put()

    post = self.activities[0]
    del post['object']['tags']
    FakeGrSource.activities = [post]

    self.post_task()

    resp = resp.key.get()
    self.assertEqual({
      'null': mention_activity,
      'http://target1/post/url': json_dumps({
        'id': 'tag:source.com,2013:a',
        'url': 'http://fa.ke/post/url',
        'object': {'content': 'foo http://target1/post/url bar'},
      }),
    }, dict(zip(json_loads(resp.urls_to_activity), resp.activities_json)))

  def test_search_links_returns_comment_with_link(self):
    """Legendary KeyError bug, https://github.com/snarfed/bridgy/issues/237"""
    source = self.sources[0]
    source.domain_urls = ['http://foo/']
    source.domains = ['foo']
    source.put()

    comment = {
      'id': 'tag:fake.com:9',
      'object': {
        'id': 'tag:fake.com:9',
        'url': 'https://twitter.com/_/status/9',
        'content': 'http://foo/post @foo',
        'author': {
          'name': 'bar',
          'id': 'tag:source:2013:bar',  # someone else
        },
        'inReplyTo': [{
          'id': 'tag:fake.com:456',
          'url': 'https://twitter.com/_/status/456',
        }],
      },
    }
    FakeGrSource.activities = []
    FakeGrSource.search_results = [comment]

    self.post_task()
    self.assert_responses([Response(
      id='tag:fake.com:9',
      type='comment',
      unsent=['http://foo/post'],
    )], ignore=('activities_json', 'urls_to_activity', 'response_json', 'source',
                'original_posts'))

  def test_quote_post_attachment(self):
    """One silo post references (quotes) another one; second should be propagated
    as a mention of the first.
    """
    source = self.sources[0]
    FakeGrSource.activities = [self.quote_post]
    self.post_task()
    self.assert_responses([Response(
      id='tag:source,2013:1234',
      type='post',
      unsent=['http://author/permalink'],
      activities_json=[json_dumps(util.prune_activity(self.quote_post, source))],
      urls_to_activity='{"http://author/permalink":0}',
    )], ignore=('response_json', 'source', 'original_posts'))

  def test_quote_post_attachment_and_user_mention(self):
    """One silo post references (quotes) another one and also user mentions the
    other post's author. We should send webmentions for both references.
    """
    source = self.sources[0]
    post = copy.deepcopy(self.quote_post)
    post['object']['tags'] = [{
      'objectType': 'person',
      'id': source.user_tag_id(),
      'urls': [{'value': 'http://author'}],
    }]

    FakeGrSource.activities = [post]
    self.post_task()
    self.assert_responses([Response(
      id='tag:source,2013:1234',
      type='post',
      unsent=['http://author', 'http://author/permalink'],
      activities_json=[json_dumps(util.prune_activity(post, source))],
      urls_to_activity='{"http://author/permalink":0,"http://author":0}',
    )], ignore=('response_json', 'source', 'original_posts'))

  def test_quote_reply_attachment(self):
    """One silo *reply* references (quotes) another post."""
    source = self.sources[0]
    reply = copy.deepcopy(self.quote_post)
    reply['object']['inReplyTo'] = [{'id': 'tag:fake.com:456'}]

    FakeGrSource.activities = []
    FakeGrSource.search_results = [reply]
    self.post_task()
    self.assert_responses([Response(
      id='tag:source,2013:1234',
      type='comment',
      unsent=['http://author/permalink'],
      activities_json=[json_dumps(util.prune_activity(reply, source))],
      urls_to_activity='{"http://author/permalink":0}',
    )], ignore=('response_json', 'source', 'original_posts'))

  def test_wrong_last_polled(self):
    """If the source doesn't have our last polled value, we should quit.
    """
    self.sources[0].last_polled = datetime.datetime.fromtimestamp(
      3, tz=datetime.timezone.utc)
    self.sources[0].put()
    self.post_task()
    self.assertEqual([], list(Response.query()))

  def test_no_source(self):
    """If the source doesn't exist, do nothing and let the task die.
    """
    self.sources[0].key.delete()
    self.post_task(expect_poll=False)

  def test_disabled_source(self):
    """If the source is disabled, do nothing and let the task die.
    """
    self.sources[0].status = 'disabled'
    self.sources[0].put()
    self.post_task(expect_poll=False)

  def test_source_without_listen_feature(self):
    """If the source doesn't have the listen feature, let the task die.
    """
    self.sources[0].features = []
    self.sources[0].put()
    self.post_task(expect_poll=False)
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

  def test_site_specific_disable_source_401(self):
    self._test_site_specific_disable_source(
      urllib.error.HTTPError('url', 401, 'msg', {}, io.StringIO('body')))

  def test_site_specific_disable_source_401_oauth(self):
    """HTTP 401 and 400 '' for Instagram should disable the source."""
    self._test_site_specific_disable_source(
      urllib.error.HTTPError('url', 400, 'foo', {}, io.StringIO(
        '{"meta":{"error_type":"OAuthAccessTokenException"}}')))

  def _test_site_specific_disable_source(self, err):
      self.expect_get_activities().AndRaise(err)
      self.mox.ReplayAll()

      self.post_task()
      self.assertEqual('disabled', self.sources[0].key.get().status)

  def test_rate_limiting_error(self):
    """Finish the task on rate limiting errors."""
    self.sources[0].RATE_LIMIT_HTTP_CODES = ('429', '456')

    error_body = json_dumps({"meta": {
      "code": 429, "error_message": "The maximum number of requests...",
      "error_type": "OAuthRateLimitException"}})
    self.expect_get_activities().AndRaise(
      urllib.error.HTTPError('url', 429, 'Rate limited', {},
                             io.StringIO(error_body)))

    self.post_task(expect_poll=FakeSource.RATE_LIMITED_POLL,
                   expect_last_polled=util.EPOCH)
    source = self.sources[0].key.get()
    self.assertEqual('error', source.poll_status)
    self.assertTrue(source.rate_limited)

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

    FakeGrSource.activities = []
    self.post_task(expect_poll=FakeSource.SLOW_POLL)

  def test_slow_poll_sent_webmention_over_month_ago(self):
    self.sources[0].created = NOW - (FakeSource.FAST_POLL_GRACE_PERIOD +
                                     datetime.timedelta(minutes=1))
    self.sources[0].last_webmention_sent = NOW - datetime.timedelta(days=32)
    self.sources[0].put()

    FakeGrSource.activities = []
    self.post_task(expect_poll=FakeSource.SLOW_POLL)

  def test_fast_poll_grace_period(self):
    self.sources[0].created = NOW - datetime.timedelta(minutes=1)
    self.sources[0].put()

    FakeGrSource.activities = []
    self.post_task(expect_poll=FakeSource.FAST_POLL)

  def test_fast_poll_hgr_sent_webmention(self):
    self.sources[0].created = NOW - (FakeSource.FAST_POLL_GRACE_PERIOD +
                                     datetime.timedelta(minutes=1))
    self.sources[0].last_webmention_sent = NOW - datetime.timedelta(days=1)
    self.sources[0].put()

    FakeGrSource.activities = []
    self.post_task(expect_poll=FakeSource.FAST_POLL)

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
    self.assertEqual(NOW, source.last_syndication_url)

  def test_multiple_activities_fetch_hfeed_once(self):
    """Make sure that multiple activities only fetch the author's h-feed once.
    """
    self.sources[0].domain_urls = ['http://author']
    self.sources[0].put()

    FakeGrSource.activities = self.activities

    # syndicated urls need to be unique for this to be interesting
    for letter, activity in zip(string.ascii_letters, FakeGrSource.activities):
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
    for letter, activity in zip(string.ascii_letters, FakeGrSource.activities):
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

    self.post_task(expect_poll=FakeSource.FAST_POLL)
    self.assertEqual(hour_ago, self.sources[0].key.get().last_hfeed_refetch)

    # should still be a blank SyndicatedPost
    relationships = SyndicatedPost.query(
      SyndicatedPost.original == 'http://author/permalink',
      ancestor=self.sources[0].key).fetch()
    self.assertEqual(1, len(relationships))
    self.assertIsNone(relationships[0].syndication)

    # should not have repropagated any responses. tasks_client is stubbed
    # out in tests, mox will complain if it gets called.

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
    self.post_task(expect_poll=FakeSource.FAST_POLL)

    # shouldn't repropagate it
    self.assertEqual('complete', resp.key.get().status)

  def test_do_refetch_hfeed(self):
    """Emulate a situation where we've done posse-post-discovery earlier and
    found no rel=syndication relationships for a particular silo URL. Every
    two hours or so, we should refetch the author's page and check to see if
    any new syndication links have been added or updated.
    """
    self._setup_refetch_hfeed()
    self._expect_fetch_hfeed()
    # should repropagate all 12 responses
    for resp in self.responses:
      self.expect_task('propagate', response_key=resp)

    self.post_task(expect_poll=FakeSource.FAST_POLL)

    # should have a new SyndicatedPost
    relationships = SyndicatedPost.query(
      SyndicatedPost.original == 'http://author/permalink',
      ancestor=self.sources[0].key).fetch()
    self.assertEqual(1, len(relationships))
    self.assertEqual('https://fa.ke/post/url', relationships[0].syndication)

    source = self.sources[0].key.get()
    self.assertEqual(NOW, source.last_syndication_url)
    self.assertEqual(NOW, source.last_hfeed_refetch)

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
    self.assertEqual(NOW, self.sources[0].key.get().last_hfeed_refetch)

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

    self.expect_task('propagate', response_key=self.responses[4])

    self.post_task(reset=True, expect_poll=FakeSource.FAST_POLL)
    self.assert_equals(replies, json_loads(source.key.get().seen_responses_cache_json))
    self.responses[4].key.delete()

    # new responses that don't include existing response. cache will have
    # existing response.
    del activity['object']['replies']
    activity['object']['tags'] = tags

    self.mox.VerifyAll()
    self.mox.UnsetStubs()
    self.mox.StubOutWithMock(tasks_client, 'create_task')
    for resp in self.responses[1:4]:
      self.expect_task('propagate', response_key=resp)

    self.post_task(reset=True, expect_poll=FakeSource.FAST_POLL)
    self.assert_equals([r.key for r in self.responses[:4]],
                       list(Response.query().iter(keys_only=True)))
    self.assert_equals(tags, json_loads(source.key.get().seen_responses_cache_json))

  def _change_response_and_poll(self):
    resp = self.responses[0].key.get() or self.responses[0]
    old_resp_jsons = [resp.response_json] + resp.old_response_jsons
    targets = resp.sent = resp.unsent
    resp.unsent = []
    resp.status = 'complete'
    resp.put()

    reply = self.activities[0]['object']['replies']['items'][0]
    reply['content'] += ' xyz'
    new_resp_json = json_dumps(reply)

    self.expect_task('propagate', response_key=resp)
    self.post_task(reset=True, expect_poll=FakeSource.FAST_POLL)

    resp = resp.key.get()
    self.assertEqual(new_resp_json, resp.response_json)
    self.assertEqual(old_resp_jsons, resp.old_response_jsons)
    self.assertEqual('new', resp.status)
    self.assertEqual(targets, resp.unsent)
    self.assertEqual([], resp.sent)

    source = self.sources[0].key.get()
    self.assert_equals([reply], json_loads(source.seen_responses_cache_json))

    self.mox.VerifyAll()
    self.mox.UnsetStubs()
    self.mox.StubOutWithMock(tasks_client, 'create_task')

  def test_in_blocklist(self):
    """Responses from blocked users should be ignored."""
    self.mox.StubOutWithMock(FakeSource, 'is_blocked')
    FakeSource.is_blocked(mox.IgnoreArg()).AndReturn(False)
    FakeSource.is_blocked(mox.IgnoreArg()).AndReturn(True)  # block second response
    FakeSource.is_blocked(mox.IgnoreArg()).MultipleTimes(10).AndReturn(False)

    expected = [self.responses[0]] + self.responses[2:]
    for resp in expected:
      self.expect_task('propagate', response_key=resp)

    self.post_task(expect_poll=FakeSource.FAST_POLL)
    self.assertEqual(11, Response.query().count())
    self.assert_responses(expected)


  def test_opt_out(self):
    """Responses from opted out users should be ignored."""
    self.activities[0]['object']['replies']['items'][0]['author'] = {
      'summary': 'foo #nobot bar',
    }
    self.activities[0]['object']['tags'][0]['actor'] = {
      'description': 'foo #nobridge bar',
    }

    expected = self.responses[2:]
    for resp in expected:
      self.expect_task('propagate', response_key=resp)

    self.post_task(expect_poll=FakeSource.FAST_POLL)
    self.assertEqual(10, Response.query().count())
    self.assert_responses(expected)


class DiscoverTest(TaskTest):

  post_url = '/_ah/queue/discover'

  def discover(self, **kwargs):
    super().post_task(params={
      'source_key': self.sources[0].key.urlsafe().decode(),
      'post_id': 'b',
    }, **kwargs)

  def test_new(self):
    """A new silo post we haven't seen before."""
    self.mox.StubOutWithMock(FakeSource, 'get_activities')
    FakeSource.get_activities(
      activity_id='b', fetch_replies=True, fetch_likes=True, fetch_shares=True,
      user_id=self.sources[0].key.id()).AndReturn([self.activities[1]])
    for resp in self.responses[4:8]:
      self.expect_task('propagate', response_key=resp)
    self.mox.ReplayAll()

    self.assertEqual(0, Response.query().count())
    self.discover()
    self.assert_responses(self.responses[4:8] + [Response(
      id=self.activities[1]['id'],
      type='post',
      source=self.sources[0].key,
      status='complete',
    )], ignore=('activities_json', 'urls_to_activity', 'response_json', 'original_posts'))

  def test_no_post(self):
    """Silo post not found."""
    self.mox.StubOutWithMock(tasks_client, 'create_task')
    FakeGrSource.activities = []
    self.discover()
    self.assert_responses([])

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
    for resp in resps:
      self.expect_task('propagate', response_key=resp)
    self.mox.ReplayAll()

    self.discover()

    for resp in Response.query():
      if resp.key.id() == self.activities[1]['id']:
        continue
      self.assert_equals('new', resp.status)
      self.assert_equals(['http://target1/post/url', 'http://target/2'],
                         resp.unsent, resp.key)

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
    self.expect_task('discover', source_key=self.sources[0], post_id='456')
    self.mox.ReplayAll()
    self.discover()

  def test_link_to_post(self):
    """If the activity links to a post, we should enqueue it itself."""
    source = self.sources[0]
    source.domain_urls = ['http://foo/']
    source.domains = ['foo']
    source.put()

    self.mox.StubOutWithMock(FakeSource, 'get_activities')
    FakeSource.get_activities(
      activity_id='b', fetch_replies=True, fetch_likes=True, fetch_shares=True,
      user_id=self.sources[0].key.id()).AndReturn([{
        'id': 'tag:fake.com:123',
        'object': {
          'author': {'id': 'tag:not-source'},
          'id': 'tag:fake.com:123',
          'url': 'https://fake.com/_/status/123',
          'content': 'i like https://foo/post a lot',
        },
      }])
    resp_key = ndb.Key('Response', 'tag:fake.com:123')
    self.expect_task('propagate', response_key=resp_key)
    self.mox.ReplayAll()

    self.discover()
    resp = resp_key.get()
    self.assert_equals('new', resp.status)
    self.assert_equals(['https://foo/post'], resp.unsent)

  def test_get_activities_error(self):
    self._test_get_activities_error(400)

  def test_get_activities_rate_limited(self):
    self._test_get_activities_error(429)

  def _test_get_activities_error(self, status):
    self.expect_get_activities(activity_id='b', user_id=self.sources[0].key.id()
        ).AndRaise(urllib.error.HTTPError('url', status, 'Rate limited', {}, None))
    self.mox.StubOutWithMock(tasks_client, 'create_task')
    self.mox.ReplayAll()

    self.discover(expected_status=ERROR_HTTP_RETURN_CODE)
    self.assert_responses([])

  def test_event_type(self):
    self.mox.StubOutWithMock(FakeGrSource, 'get_event')
    FakeGrSource.get_event('321').AndReturn(self.activities[0])
    for resp in self.responses[:4]:
      self.expect_task('propagate', response_key=resp)
    self.mox.ReplayAll()

    self.post_task(params={
      'source_key': self.sources[0].key.urlsafe().decode(),
      'post_id': '321',
      'type': 'event',
    })
    self.assert_responses(self.responses[:4] + [Response(
      id=self.activities[0]['id'],
      type='post',
      source=self.sources[0].key,
      status='complete',
    )], ignore=('activities_json', 'urls_to_activity', 'response_json', 'original_posts'))


class PropagateTest(TaskTest):

  post_url = '/_ah/queue/propagate'

  def setUp(self):
    super().setUp()
    for r in self.responses[:4]:
      r.put()

  def post_task(self, expected_status=200, response=None, **kwargs):
    if response is None:
      response = self.responses[0]
    super().post_task(
      expected_status=expected_status,
      params={'response_key': response.key.urlsafe().decode()},
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
                        endpoint='http://webmention/endpoint',
                        discover=True, send=None, discover_status=200,
                        send_status=200, **kwargs):
    if source_url is None:
      source_url = f'http://localhost/comment/fake/{self.sources[0].key.string_id()}/a/1_2_a'

    # discover
    if discover:
      html = f'<html><link rel="webmention" href="{endpoint or ""}"></html>'
      call = self.expect_requests_get(target, html, status_code=discover_status,
                                      **kwargs).InAnyOrder()

    # send
    if send:
      assert endpoint
    if send or (send is None and endpoint):
      call = self.expect_requests_post(endpoint, data={
        'source': source_url,
        'target': target,
      }, status_code=send_status, allow_redirects=False,
        timeout=tasks.WEBMENTION_SEND_TIMEOUT.total_seconds(), **kwargs,
      ).InAnyOrder()

    return call

  def test_propagate(self):
    """Normal propagate tasks."""
    self.assertEqual('new', self.responses[0].status)

    id = self.sources[0].key.string_id()
    for url in (
        f'http://localhost/comment/fake/{id}/a/1_2_a',
        f'http://localhost/like/fake/{id}/a/alice',
        f'http://localhost/repost/fake/{id}/a/bob',
        f'http://localhost/react/fake/{id}/a/bob/a_scissors_by_bob',
    ):
      self.expect_webmention(source_url=url)
    self.mox.ReplayAll()

    now = NOW
    util.now = lambda: now

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

    self.expect_webmention()
    self.mox.ReplayAll()
    self.post_task()
    self.assert_response_is('complete', NOW + LEASE_LENGTH,
                            sent=['http://target1/post/url'])
    self.assert_equals(NOW, self.sources[0].key.get().last_webmention_sent)

  def test_success_and_errors(self):
    """We should send webmentions to the unsent and error targets."""
    self.responses[0].unsent = ['http://1', 'http://2', 'http://3', 'http://8']
    self.responses[0].error = ['http://4', 'http://5', 'http://6', 'http://9']
    self.responses[0].sent = ['http://7']
    self.responses[0].put()

    self.expect_webmention(target='http://1')
    self.expect_webmention(target='http://8', discover_status=204)
    self.expect_webmention(target='http://2', endpoint=None)
    self.expect_webmention(target='http://3', send_status=500)
    # 4XX should go into 'failed'
    self.expect_webmention(target='http://4', send_status=404)
    self.expect_webmention(target='http://5', send_status=403)
    # 5XX and 429 should go into 'error'
    self.expect_webmention(target='http://6', send_status=500)
    self.expect_webmention(target='http://9', send_status=429)

    self.mox.ReplayAll()
    self.post_task(expected_status=ERROR_HTTP_RETURN_CODE)
    self.assert_response_is('error',
                            sent=['http://7', 'http://1', 'http://8'],
                            error=['http://3', 'http://6', 'http://9'],
                            failed=['http://4', 'http://5'],
                            skipped=['http://2'])
    self.assertEqual(NOW, self.sources[0].key.get().last_webmention_sent)

  def test_cached_webmention_discovery(self):
    """Webmention endpoints should be cached."""
    self.expect_webmention()
    # second webmention should use the cached endpoint
    self.expect_webmention(discover=False)

    self.mox.ReplayAll()
    self.post_task()

    self.responses[0].status = 'new'
    self.responses[0].put()
    self.post_task()

  def test_cached_webmention_discovery_error(self):
    """Failed webmention discovery should be cached too."""
    self.expect_webmention(endpoint=None)
    # second time shouldn't try to send a webmention

    self.mox.ReplayAll()
    self.post_task()
    self.assert_response_is('complete', skipped=['http://target1/post/url'])

    self.responses[0].status = 'new'
    self.responses[0].put()
    self.post_task()
    self.assert_response_is('complete', skipped=['http://target1/post/url'])

  def test_errors_and_caching_endpoint(self):
    """Only cache on wm endpoint failures, not discovery failures."""
    self.expect_webmention(send=False).AndRaise(requests.ConnectionError())
    # shouldn't have a cached endpoint
    self.expect_webmention(send_status=500)
    # should have and use a cached endpoint
    self.expect_webmention(discover=False)
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
    self.expect_webmention(endpoint=None)
    self.expect_webmention()
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

  def test_webmention_blocklist(self):
    """Target URLs with domains in the blocklist should be ignored."""
    self.responses[0].unsent = ['http://t.co/bad', 'http://foo/good', 'bad url']
    self.responses[0].error = ['http://instagr.am/bad',
                               # urlparse raises ValueError: Invalid IPv6 URL
                               'http://foo]']
    self.responses[0].put()

    self.expect_webmention(target='http://foo/good')
    self.mox.ReplayAll()

    self.post_task()
    self.assert_response_is('complete', sent=['http://foo/good'])

  def test_non_html_url(self):
    """Target URLs that aren't HTML should be ignored."""
    self.expect_requests_head('http://target1/post/url',
                              content_type='application/mpeg')
    self.mox.ReplayAll()
    self.post_task()
    self.assert_response_is('complete')

  def test_non_html_file(self):
    """If our HEAD fails, we should still require content-type text/html."""
    self.expect_requests_head('http://target1/post/url', status_code=405)
    self.expect_webmention(content_type='image/gif', send=False)

    self.mox.ReplayAll()
    self.post_task()
    self.assert_response_is('complete', skipped=['http://target1/post/url'])

  def test_non_html_file_extension(self):
    """If our HEAD fails, we should infer type from file extension."""
    self.responses[0].unsent = ['http://this/is/a.pdf']
    self.responses[0].put()

    self.expect_webmention(target='http://this/is/a.pdf', send_status=405,
                           # we should ignore an error response's content type
                           content_type='text/html')

    self.mox.ReplayAll()
    self.post_task()
    self.assert_response_is('complete', failed=['http://this/is/a.pdf'])

  def test_content_type_html_with_charset(self):
    """We should handle Content-Type: text/html; charset=... ok."""
    self.expect_webmention(content_type='text/html; charset=utf-8')
    self.mox.ReplayAll()
    self.post_task()
    self.assert_response_is('complete', sent=['http://target1/post/url'])

  def test_no_content_type_header(self):
    """If the Content-Type header is missing, we should assume text/html."""
    self.expect_webmention(content_type=None)
    self.mox.ReplayAll()
    self.post_task()
    self.assert_response_is('complete', sent=['http://target1/post/url'])

  def test_link_header_rel_webmention_unquoted(self):
    """We should support rel=webmention (no quotes) in the Link header."""
    self.expect_webmention(
      response_headers={'Link': '<http://webmention/endpoint>; rel=webmention'})
    self.mox.ReplayAll()
    self.post_task()
    self.assert_response_is('complete', sent=['http://target1/post/url'])

  def test_webmention_post_accept_header(self):
    """The webmention POST request should send Accept: */*."""
    self.expect_requests_get(
      'http://target1/post/url', timeout=15,
      response_headers={'Link': '<http://my/endpoint>; rel=webmention'})

    self.expect_requests_post(
      'http://my/endpoint', timeout=tasks.WEBMENTION_SEND_TIMEOUT.total_seconds(),
      data={'source': 'http://localhost/comment/fake/0123456789/a/1_2_a',
            'target': 'http://target1/post/url'},
      allow_redirects=False, headers={'Accept': '*/*'})

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
    url = 'https://maps/?q=' + urllib.parse.quote_plus('3 Cours de la République'.encode())
    self.responses[0].unsent = [url]
    self.responses[0].put()

    self.expect_webmention(target=url)
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
    self.expect_webmention(target='http://bar/1', endpoint='http://no')
    # target is in source.domains
    self.expect_webmention(target='http://foo/2', endpoint='http://yes')

    self.mox.ReplayAll()
    self.post_task()
    self.assert_equals('http://yes', self.sources[0].key.get().webmention_endpoint)

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

    self.expect_webmention()
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

  def test_non_public_response_author(self):
    """If the response's author is non-public, we should give up."""
    resp = json_loads(self.responses[0].response_json)
    resp['author'] = {
      'id': 'tag:source.com,2013:alice',
      'to': [{'objectType':'group', 'alias':'@private'}],
    }
    self.responses[0].response_json = json_dumps(resp)
    self.responses[0].put()

    self.post_task()
    self.assert_response_is('complete', unsent=['http://target1/post/url'], sent=[])

  def test_webmention_no_endpoint(self):
    self.expect_webmention(endpoint=None)
    self.mox.ReplayAll()
    self.post_task()
    self.assert_response_is('complete', skipped=['http://target1/post/url'])

  def test_webmention_discover_400(self):
    self.expect_webmention(discover_status=400)
    self.mox.ReplayAll()
    self.post_task()
    self.assert_response_is('complete', sent=['http://target1/post/url'])

  def test_webmention_send_400(self):
    self.expect_webmention(send_status=400)
    self.mox.ReplayAll()
    self.post_task()
    self.assert_response_is('complete', failed=['http://target1/post/url'])

  def test_webmention_discover_500(self):
    self.expect_webmention(discover_status=500)
    self.mox.ReplayAll()
    self.post_task()
    self.assert_response_is('complete', sent=['http://target1/post/url'])

  def test_webmention_send_500(self):
    self.expect_webmention(send_status=500)
    self.mox.ReplayAll()
    self.post_task(expected_status=ERROR_HTTP_RETURN_CODE)
    self.assert_response_is('error', error=['http://target1/post/url'])

  def test_webmention_bad_target_url(self):
    self.responses[0].unsent = ['not a url']
    self.responses[0].put()
    self.post_task()
    self.assert_response_is('complete')

  def test_webmention_fail_and_succeed(self):
    """All webmentions should be attempted, but any failure sets error status."""
    self.responses[0].unsent = ['http://first', 'http://second']
    self.responses[0].put()
    self.expect_webmention(target='http://first', send_status=500)
    self.expect_webmention(target='http://second')

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
    self.expect_webmention(target='http://good')
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
    self.expect_webmention(send=False).AndRaise(
      requests.exceptions.ConnectionError('DNS lookup failed for URL: foo'))
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

  def test_translate_appspot_to_brid_gy(self):
    """Tasks on brid-gy.appspot.com should translate source URLs to brid.gy."""
    self.responses[0].unsent = ['http://good']
    self.responses[0].put()
    source_url = f'https://brid.gy/comment/fake/{self.sources[0].key.string_id()}/a/1_2_a'
    self.expect_webmention(source_url=source_url, target='http://good')

    self.mox.ReplayAll()
    self.post_task(base_url='http://brid-gy.appspot.com')

  def test_activity_id_not_tag_uri(self):
    """If the activity id isn't a tag uri, we should just use it verbatim."""
    activity = json_loads(self.responses[0].activities_json[0])
    activity['id'] = 'AAA'
    self.responses[0].activities_json = [json_dumps(activity)]

    self.responses[0].unsent = ['http://good']
    self.responses[0].put()

    source_url = f'https://brid.gy/comment/fake/{self.sources[0].key.string_id()}/AAA/1_2_a'
    self.expect_webmention(source_url=source_url, target='http://good')

    self.mox.ReplayAll()
    self.post_task(base_url='https://brid.gy')

  def test_response_with_multiple_activities(self):
    """Should use Response.urls_to_activity to generate the source URLs."""
    self.responses[0].activities_json = [
      '{"id": "000"}', '{"id": "111"}', '{"id": "222"}']
    self.responses[0].unsent = ['http://AAA', 'http://BBB', 'http://CCC']
    self.responses[0].urls_to_activity = json_dumps(
      {'http://AAA': 0, 'http://BBB': 1, 'http://CCC': 2})
    self.responses[0].put()

    source_url = f'https://brid.gy/comment/fake/{self.sources[0].key.string_id()}/%s/1_2_a'
    self.expect_webmention(source_url=source_url % '000', target='http://AAA')
    self.expect_webmention(source_url=source_url % '111', target='http://BBB')
    self.expect_webmention(source_url=source_url % '222', target='http://CCC')

    self.mox.ReplayAll()
    self.post_task(base_url='https://brid.gy')

  def test_response_with_single_activity(self):
    """Should use the activity to generate source URL, even if urls_to_activity is unset."""
    self.responses[0].activities_json = ['{"id": "000"}']
    self.responses[0].unsent = ['http://AAA']
    self.responses[0].urls_to_activity = None
    self.responses[0].put()

    self.expect_webmention(
      source_url=f'https://brid.gy/comment/fake/{self.sources[0].key.string_id()}/000/1_2_a',
      target='http://AAA')

    self.mox.ReplayAll()
    self.post_task(base_url='https://brid.gy')

  def test_complete_exception(self):
    """If completing raises an exception, the lease should be released."""
    self.expect_webmention()
    self.mox.StubOutWithMock(tasks.PropagateResponse, 'complete')
    tasks.PropagateResponse.complete().AndRaise(Exception('foo'))
    self.mox.ReplayAll()

    self.post_task(expected_status=500)
    self.assert_response_is('error', None, sent=['http://target1/post/url'])

  def test_source_url_key_error(self):
    """We should gracefully retry when we hit the KeyError bug.

    ...or any other exception outside the per-webmention try/except,
    eg from source_url().

    https://github.com/snarfed/bridgy/issues/237
    """
    orig = list(self.responses[0].unsent)
    self.responses[0].urls_to_activity = json_dumps({'bad': 9})
    self.responses[0].put()
    self.mox.ReplayAll()
    self.post_task(expected_status=ERROR_HTTP_RETURN_CODE)
    self.assert_response_is('error', error=orig)

  def test_source_url_empty_activities_json(self):
    """If Response.activities_json is empty, we should use the response itself.

    https://github.com/snarfed/bridgy/issues/237
    """
    self.responses[0].activities_json = []
    self.responses[0].put()

    self.expect_webmention(source_url='http://localhost/comment/fake/0123456789/1_2_a/1_2_a')
    self.mox.ReplayAll()

    self.post_task()
    self.assert_response_is('complete', sent=['http://target1/post/url'])

  def test_source_url_missing_in_urls_to_activity(self):
    """If the source URL isn't in Response.urls_to_activity, use the response.

    https://github.com/snarfed/bridgy/issues/237
    """
    self.responses[0].activities_json = [json_dumps({
      'id': 'tag:fake.com:555',
      'object': {'content': 'http://activity/post'},
    })]
    self.responses[0].urls_to_activity = json_dumps({'http://activity/post': 0})
    self.responses[0].put()

    self.expect_webmention(source_url='http://localhost/comment/fake/0123456789/555/1_2_a')
    self.mox.ReplayAll()

    self.post_task()
    self.assert_response_is('complete', sent=['http://target1/post/url'])

  def test_propagate_blogpost(self):
    """Blog post propagate task."""
    source_key = FakeSource.new(domains=['fake']).put()
    links = ['http://fake/post', '/no/domain', 'http://ok/one.png',
             'http://ok/two', 'http://ok/two', # repeated
             ]
    blogpost = models.BlogPost(id='http://x', source=source_key, unsent=links)
    blogpost.put()

    self.expect_requests_head('http://fake/post')
    self.expect_requests_head('http://ok/one.png', content_type='image/png')
    self.expect_requests_head('http://ok/two')
    self.expect_webmention(source_url='http://x', target='http://ok/two')
    self.mox.ReplayAll()

    self.post_url = '/_ah/queue/propagate-blogpost'
    super().post_task(params={'key': blogpost.key.urlsafe().decode()})
    self.assert_response_is('complete', NOW + LEASE_LENGTH,
                            sent=['http://ok/two'], response=blogpost)
    self.assert_equals(NOW, source_key.get().last_webmention_sent)

  def test_propagate_blogpost_allows_bridgy_publish_links(self):
    source_key = FakeSource.new(domains=['fake']).put()
    blogpost = models.BlogPost(id='http://x', source=source_key,
                               unsent=['https://brid.gy/publish/twitter'])
    blogpost.put()

    self.expect_requests_head('https://brid.gy/publish/twitter')
    self.expect_webmention(
      source_url='http://x',
      target='https://brid.gy/publish/twitter',
      endpoint='https://brid.gy/publish/webmention')
    self.mox.ReplayAll()

    self.post_url = '/_ah/queue/propagate-blogpost'
    super().post_task(params={'key': blogpost.key.urlsafe().decode()})
    self.assert_response_is('complete', response=blogpost,
                            sent=['https://brid.gy/publish/twitter'])

  def test_propagate_blogpost_follows_redirects_before_checking_self_link(self):
    source_key = FakeSource.new(domains=['fake']).put()
    blogpost = models.BlogPost(id='http://x', source=source_key,
                               unsent=['http://will/redirect'])
    blogpost.put()

    self.expect_requests_head('http://will/redirect',
                              redirected_url='http://www.fake/self/link')
    self.mox.ReplayAll()

    self.post_url = '/_ah/queue/propagate-blogpost'
    super().post_task(params={'key': blogpost.key.urlsafe().decode()})
    self.assert_response_is('complete', response=blogpost)

  def test_post_response(self):
    """Responses with type 'post' (ie mentions) are their own activity.

    https://github.com/snarfed/bridgy/issues/456
    """
    self.responses[0].type = 'post'
    self.responses[0].response_json = json_dumps(json_loads(
      self.responses[0].activities_json[0]))
    self.responses[0].put()

    self.expect_webmention(source_url='http://localhost/post/fake/0123456789/a')
    self.mox.ReplayAll()
    self.post_task()


class PropagateBlogPostTest(TaskTest):

  post_url = '/_ah/queue/propagate-blogpost'

  def setUp(self):
    super().setUp()
    self.blogposts[0].unsent = ['http://foo', 'http://bar']
    self.blogposts[0].status = 'new'
    self.blogposts[0].put()

  def post_task(self, **kwargs):
    super().post_task(params={'key': self.blogposts[0].key.urlsafe().decode()},
                      **kwargs)

  def test_no_source(self):
    """If the source doesn't exist, do nothing and let the task die."""
    self.sources[0].key.delete()
    self.post_task()

  def test_disabled_source(self):
    """If the source is disabled, do nothing and let the task die."""
    self.sources[0].status = 'disabled'
    self.sources[0].put()
    self.post_task()
