"""Unit test utilities."""
import copy
from datetime import datetime, timedelta, timezone
import logging
import re
import urllib.request, urllib.parse, urllib.error

from webutil.appengine_config import ndb_client, tasks_client

from google.cloud import ndb
from google.cloud.tasks_v2.types import Task
from granary import as1
from granary import source as gr_source
from unittest.mock import patch

from oauth_dropins import views as oauth_views
from oauth_dropins.models import BaseAuth
from webutil import testutil
from webutil.testutil import NOW, requests_response, UrlopenResult
from webutil import util as webutil_util
from webutil.util import json_dumps, json_loads
import requests
from requests import post as orig_requests_post

import flask_app, flask_background, util
from models import BlogPost, Publish, PublishedPage, Response, Source

logger = logging.getLogger(__name__)


class FakeAuthEntity(BaseAuth):
  user_json = ndb.TextProperty()
  access_token_str = ndb.StringProperty()

  def access_token(self):
    return self.access_token_str


class FakeGrSource(gr_source.Source):
  """Fake granary source class.

  Attributes:
    * activities
    * actor
    * blocked_ids
    * comment
    * etag
    * event
    * last_search_query
    * like
    * reaction
    * rsvp
    * search_results
    * share
  """
  NAME = 'FakeSource'
  DOMAIN = 'fa.ke'

  last_search_query = None
  search_results = []

  def user_url(self, id):
    return 'http://fa.ke/' + id

  def to_as1_actor(self, user):
    return user

  def get_comment(self, *args, **kwargs):
    return copy.deepcopy(self.comment)

  def get_like(self, *args, **kwargs):
    return copy.deepcopy(self.like)

  def get_reaction(self, *args, **kwargs):
    return copy.deepcopy(self.reaction)

  def get_share(self, *args, **kwargs):
    return copy.deepcopy(self.share)

  def get_event(self, *args, **kwargs):
    return copy.deepcopy(self.event)

  def get_rsvp(self, *args, **kwargs):
    return copy.deepcopy(self.rsvp)

  def get_blocklist_ids(self, *args, **kwargs):
    return copy.deepcopy(self.blocklist_ids)

  def is_blocked(self, obj):
    return obj.get('author', {}).get('id') in self.blocklist_ids

  @classmethod
  def clear(cls):
    cls.like = cls.reaction = cls.share = cls.event = \
      cls.rsvp = cls.etag = cls.last_search_query = None
    cls.activities = cls.search_results = cls.blocklist_ids = []

  def get_activities_response(self, user_id=None, group_id=None,
                              activity_id=None, app_id=None,
                              fetch_replies=False, fetch_likes=False,
                              fetch_shares=False, fetch_mentions=False,
                              count=None, etag=None, min_id=None, cache=None,
                              search_query=None):
    activities = self.activities
    if search_query is not None:
      assert group_id == gr_source.SEARCH
      FakeGrSource.last_search_query = search_query
      activities = self.search_results

    activities = copy.deepcopy(activities)
    for activity in activities:
      obj = activity.get('object') or activity
      obj['tags'] = [tag for tag in obj.get('tags', []) if
                     'verb' not in tag or
                     (tag['verb'] in ('like', 'react') and fetch_likes) or
                     (tag['verb'] == 'share' and fetch_shares) or
                     (tag['verb'] == 'mention' and fetch_mentions)]
      if not fetch_replies:
        obj.pop('replies', None)

    return {
      'items': util.trim_nulls(activities),
      'etag': getattr(self, 'etag', None),
    }

  def scraped_to_activities(self, scraped, count=None, fetch_extras=False):
    activities = self.get_activities(
      count=count, fetch_replies=fetch_extras, fetch_likes=fetch_extras,
      fetch_shares=fetch_extras, fetch_mentions=fetch_extras)
    return activities, self.actor

  def scraped_to_activity(self, scraped):
    activities = self.get_activities(count=1, fetch_replies=True,
                                     fetch_likes=True, fetch_shares=True)
    return activities[0] if activities else None, self.actor

  def scraped_to_actor(self, scraped):
    return self.actor

  def merge_scraped_reactions(self, scraped, activity):
    likes = json_loads(scraped)
    as1.merge_by_id(activity['object'], 'tags', likes)
    return likes

  def merge_scraped_comments(self, scraped, activity):
    comments = json_loads(scraped)
    as1.merge_by_id(activity['object'].setdefault('replies', {}), 'items', comments)
    return comments

  def create(self, obj, include_link=gr_source.OMIT_LINK,
             ignore_formatting=False):
    verb = obj.get('verb')
    type = obj.get('objectType')
    if verb == 'like':
      return gr_source.creation_result(
        abort=True, error_plain='Cannot publish likes',
        error_html='Cannot publish likes')
    if 'content' not in obj:
      return gr_source.creation_result(
        abort=False, error_plain='No content',
        error_html='No content')

    if type == 'comment':
      base_url = self.base_object(obj).get('url')
      if not base_url:
        return gr_source.creation_result(
          abort=True,
          error_plain=f'no {self.DOMAIN} url to reply to',
          error_html=f'no {self.DOMAIN} url to reply to')

    content = self._content_for_create(obj, ignore_formatting=ignore_formatting)
    if include_link == gr_source.INCLUDE_LINK:
        content += f' - {obj["url"]}'
    ret = {
      'id': 'fake id',
      'url': 'http://fake/url',
      'content': content,
      'granary_message': 'granary message',
    }
    if verb == 'rsvp-yes':
      ret['type'] = 'post'

    images = self._images(obj)
    if images:
      ret['images'] = images

    return gr_source.creation_result(ret)

  def preview_create(self, obj, include_link=gr_source.OMIT_LINK,
                     ignore_formatting=False):
    if obj.get('verb') == 'like':
      return gr_source.creation_result(
        abort=True, error_plain='Cannot publish likes',
        error_html='Cannot publish likes')

    content = self._content_for_create(obj, ignore_formatting=ignore_formatting)
    if include_link == gr_source.INCLUDE_LINK:
        content += f" - {obj['url']}"

    content = 'preview of ' + content

    images = self._images(obj)
    if images:
      content += f" with images {','.join(images)}"

    return gr_source.creation_result(description=content)

  def delete(self, id):
    return gr_source.creation_result({
      'url': 'http://fake/url',
      'msg': f'delete {id}',
    })

  def preview_delete(self, id):
    return gr_source.creation_result(description=f'delete {id}')

  @staticmethod
  def _images(obj):
    return [image['url'] for image in util.get_list(obj, 'image')]


class OAuthStart(oauth_views.Start):
  """Stand-in for the oauth-dropins Start, redirects to a made-up silo url."""
  def redirect_url(self, state=None):
    logger.debug(f'oauth view redirect with state {state}')
    return 'http://fake/auth/url?' + urllib.parse.urlencode({
      'redirect_uri': self.to_url(state),
    })

FakeStart = util.oauth_starter(OAuthStart)


class FakeSource(Source):
  GR_CLASS = FakeGrSource
  OAUTH_START = FakeStart
  AUTH_MODEL = FakeAuthEntity
  SHORT_NAME = 'fake'
  TYPE_LABELS = {'post': 'FakeSource post label'}
  RATE_LIMITED_POLL = timedelta(hours=30)
  URL_CANONICALIZER = util.UrlCanonicalizer(domain=GR_CLASS.DOMAIN,
                                            # test these code paths
                                            approve='NO', reject='NO')
  PATH_BLOCKLIST = (re.compile('^/blocklisted/.*'),)
  HAS_BLOCKS = True
  CAN_PUBLISH = True
  MICROPUB_TOKEN_PROPERTY = 'access_token_str'

  string_id_counter = 1
  gr_source = FakeGrSource()
  is_saved = False

  def is_beta_user(self):
    return True

  def silo_url(self):
    return 'http://fa.ke/profile/url'

  def feed_url(self):
    return 'fake feed url'

  def search_for_links(self):
    return copy.deepcopy(FakeGrSource.search_results)

  @classmethod
  def new(cls, **props):
    id = None
    if 'url' not in props:
      props['url'] = 'http://fake/url'
    auth_entity = props.get('auth_entity')
    if auth_entity:
      props['auth_entity'] = auth_entity.key
      if auth_entity.user_json:
        user_obj = json_loads(auth_entity.user_json)
        if 'name' not in props:
          props['name'] = user_obj.get('name')
        id = user_obj.get('id')
    if not props.get('name'):
      props['name'] = 'fake'
    if not id:
      id = cls.string_id_counter
      cls.string_id_counter += 1
    return cls(id=str(id), **props)

  def put(self, **kwargs):
    self.is_saved = True
    return super().put(**kwargs)

  @classmethod
  def next_key(cls):
    return ndb.Key(cls, str(cls.string_id_counter))

  def create_comment(self, *args, **kwargs):
    return {'id': 'fake id'}


class FakeBlogSource(FakeSource):
  SHORT_NAME = 'fake_blog'


class TestCase(testutil.TestCase):
  """Base test class. Sets up Flask client and test data.

  Use AppTest or BackgroundTest instead!

  Attributes:
    client: :class:`werkzeug.test.Client`
    sources: list of FakeSource
    responses: list of unsaved Response
    publishes: list of one unsaved Publish
    blogposts: list of one unsaved BlogPost
  """
  app = None  # overridden by subclasses

  def setUp(self):
    super().setUp()
    FakeGrSource.clear()
    FakeSource.create_comment_calls = []

    self.mock_get = self.start_patch(util.session, 'get',
                                     return_value=requests_response(''))
    self.mock_post = self.start_patch(util.session, 'post',
                                      return_value=requests_response(''))
    self.mock_head = self.start_patch(util.session, 'head',
                                      side_effect=lambda url, **kw: requests_response('', url=url))
    self.mock_urlopen = self.start_patch(util.urllib.request, 'urlopen',
                                         return_value=UrlopenResult(200, ''))

    # add FakeSource everywhere necessary
    util.BLOCKLIST.add('fa.ke')

    util.webmention_endpoint_cache.clear()
    self.mock_create_task = self.start_patch(tasks_client, 'create_task',
                                             return_value=Task(name='my task'))

    self.client = self.app.test_client()
    self.client.__enter__()

    self.clear_datastore()
    self.ndb_context = ndb_client.context()
    self.ndb_context.__enter__()

    # webutil's testutil.TestCase.setUp() updated this, so make sure we update
    # our util's to match
    util.now = webutil_util.now

    # sources
    self.auth_entities = [
      FakeAuthEntity(
        key=ndb.Key('FakeAuthEntity', '01122334455'),
        user_json=json_dumps({
          'id': '0123456789',
          'name': 'Fake User',
          'url': 'http://fakeuser.com/',
        })),
      FakeAuthEntity(
        key=ndb.Key('FakeAuthEntity', '0022446688'),
        user_json=json_dumps({
          'id': '0022446688',
          'name': 'Another Fake',
          'url': 'http://anotherfake.com/',
        }))
    ]

    self.sources = [FakeSource.new(auth_entity=self.auth_entities[0]),
                    FakeSource.new(auth_entity=self.auth_entities[1])]
    for entity in self.sources:
      entity.features = ['listen']

    with self.app.test_request_context():
      self.source_bridgy_url = self.sources[0].bridgy_url()

    self.actor = FakeGrSource.actor = {
      'objectType': 'person',
      'id': 'tag:fa.ke,2013:212038',
      'username': 'snarfed',
      'displayName': 'Ryan B',
      'url': 'https://snarfed.org/',
      'image': {'url': 'http://pic.ture/url'},
    }

    # activities
    self.activities = FakeGrSource.activities = [{
      'id': f'tag:source.com,2013:{id}',
      'url': 'http://fa.ke/post/url',
      'object': {
        'objectType': 'note',
        'id': f'tag:source.com,2013:{id}',
        'url': 'http://fa.ke/post/url',
        'content': 'foo http://target1/post/url bar',
        'to': [{'objectType':'group', 'alias':'@public'}],
        'replies': {
          'items': [{
            'objectType': 'comment',
            'id': f'tag:source.com,2013:1_2_{id}',
            'url': 'http://fa.ke/comment/url',
            'content': 'foo bar',
          }],
          'totalItems': 1,
        },
        'tags': [{
          'objectType': 'activity',
          'verb': 'like',
          'id': f'tag:source.com,2013:{id}_liked_by_alice',
          'object': {'url': 'http://example.com/abc'},
          'author': {
            'id': 'tag:source.com,2013:alice',
            'url': 'http://example.com/alice',
          },
        }, {
          'id': f'tag:source.com,2013:{id}_reposted_by_bob',
          'objectType': 'activity',
          'verb': 'share',
          'object': {'url': 'http://example.com/def'},
          'author': {'url': 'http://example.com/bob'},
        }, {
          'id': f'tag:source.com,2013:{id}_scissors_by_bob',
          'objectType': 'activity',
          'verb': 'react',
          'content': '✁',
          'object': {'url': 'http://example.com/def'},
          'author': {'url': 'http://example.com/bob'},
        }],
      },
    } for id in ('a', 'b', 'c')]

    # responses
    self.responses = []
    created = util.now() - timedelta(days=10)

    for activity in self.activities:
      obj = activity['object']
      pruned_activity = {
        'id': activity['id'],
        'url': 'http://fa.ke/post/url',
        'object': {
          'content': 'foo http://target1/post/url bar',
        }
      }

      comment = obj['replies']['items'][0]
      self.responses.append(Response(
          id=comment['id'],
          activities_json=[json_dumps(pruned_activity)],
          urls_to_activity='{"http://target1/post/url":0}',
          response_json=json_dumps(comment),
          type='comment',
          source=self.sources[0].key,
          unsent=['http://target1/post/url'],
          created=created))

      created += timedelta(hours=1)

      like = obj['tags'][0]
      self.responses.append(Response(
          id=like['id'],
          activities_json=[json_dumps(pruned_activity)],
          urls_to_activity='{"http://target1/post/url":0}',
          response_json=json_dumps(like),
          type='like',
          source=self.sources[0].key,
          unsent=['http://target1/post/url'],
          created=created))

      created += timedelta(hours=1)

      share = obj['tags'][1]
      self.responses.append(Response(
          id=share['id'],
          activities_json=[json_dumps(pruned_activity)],
          urls_to_activity='{"http://target1/post/url":0}',
          response_json=json_dumps(share),
          type='repost',
          source=self.sources[0].key,
          unsent=['http://target1/post/url'],
          created=created))

      created += timedelta(hours=1)

      reaction = obj['tags'][2]
      self.responses.append(Response(
          id=reaction['id'],
          activities_json=[json_dumps(pruned_activity)],
          urls_to_activity='{"http://target1/post/url":0}',
          response_json=json_dumps(reaction),
          type='react',
          source=self.sources[0].key,
          unsent=['http://target1/post/url'],
          created=created))

      created += timedelta(hours=1)

    # publishes
    self.publishes = [Publish(
      parent=PublishedPage(id='https://post').key,
      source=self.sources[0].key,
      status='complete',
      published={'url': 'http://fa.ke/syndpost'},
    )]

    # blogposts
    self.blogposts = [BlogPost(
      id='https://post',
      source=self.sources[0].key,
      status='complete',
      feed_item={'title': 'a post'},
      sent=['http://a/link'],
    )]

  def tearDown(self):
    self.ndb_context.__exit__(None, None, None)
    self.client.__exit__(None, None, None)
    super().tearDown()

  @staticmethod
  def clear_datastore():
    resp = orig_requests_post(f'http://0.0.0.0:8089/reset')
    resp.raise_for_status()

  def _task_spec(self, create_task_request):
    """Converts a tasks_client.create_task call's request into a comparable
    dict: queue name and request body kwargs.
    """
    queue = create_task_request.parent.rsplit('/', 1)[-1]
    body = create_task_request.task.app_engine_http_request.body.decode()
    return {'queue': queue, **dict(urllib.parse.parse_qsl(body))}

  def assert_tasks(self, *expected):
    """Asserts that exactly these tasks were created since the last
    assert_task(s) call, in any order, then resets the mock's call history.

    This is way too over-engineered. TODO: simplify.

    Args:
      expected: dicts with a 'queue' key, the expected request body kwargs,
        and optionally 'eta_seconds' to check the task's schedule time
        approximately, eg {'queue': 'propagate', 'response_key': response}.
    """
    actual = [c.args[0] for c in self.mock_create_task.call_args_list]
    actual_specs = [self._task_spec(a) for a in actual]

    expected_etas = {}
    expected_specs = []
    for i, exp in enumerate(expected):
      exp = dict(exp)
      eta_seconds = exp.pop('eta_seconds', None)
      if eta_seconds is not None:
        expected_etas[i] = eta_seconds
      for name, val in exp.items():
        if isinstance(val, ndb.Model):
          exp[name] = val.key.urlsafe().decode()
        elif isinstance(val, ndb.Key):
          exp[name] = val.urlsafe().decode()
      expected_specs.append(exp)

    self.assertCountEqual(expected_specs, actual_specs)

    for i, eta_seconds in expected_etas.items():
      task = actual[actual_specs.index(expected_specs[i])]
      got_eta = (util.to_utc_timestamp(task.task.schedule_time) -
                 util.to_utc_timestamp(util.now()))
      delta = eta_seconds * .2 + 10
      self.assertTrue(got_eta + delta >= eta_seconds >= got_eta - delta,
                      f'{eta_seconds} !~= {got_eta}')

    self.mock_create_task.reset_mock()

  def assert_task(self, queue, eta_seconds=None, **kwargs):
    """Asserts that exactly one task was created, with this queue and kwargs."""
    spec = {'queue': queue, **kwargs}
    if eta_seconds is not None:
      spec['eta_seconds'] = eta_seconds
    self.assert_tasks(spec)


class AppTest(TestCase):
  app = flask_app.app


class BackgroundTest(TestCase):
  app = flask_background.app
