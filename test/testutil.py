"""Unit test utilities.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import copy
import datetime
import json
import logging
import urllib

import appengine_config

from granary import source as gr_source
from granary import testutil as gr_testutil
from google.appengine.datastore import datastore_stub_util
from google.appengine.ext import ndb
from models import Response, Source
from oauth_dropins.models import BaseAuth
# mirror some methods from webutil.testutil
from oauth_dropins import handlers as oauth_handlers
from oauth_dropins.webutil.testutil import get_task_eta, get_task_params
import requests

import util

NOW = datetime.datetime.utcnow()


class FakeAuthEntity(BaseAuth):
  user_json = ndb.TextProperty()


class FakeGrSource(gr_source.Source):
  """Fake granary source class.

  Attributes:
    activities, like, share, event, rsvp, etag, search_results, last_search_query
  """
  NAME = 'FakeSource'
  DOMAIN = 'fa.ke'

  last_search_query = None

  def user_url(self, id):
    return 'http://fa.ke/' + id

  def user_to_actor(self, user):
    return user

  def get_comment(self, *args, **kwargs):
    return copy.deepcopy(self.comment)

  def get_like(self, *args, **kwargs):
    return copy.deepcopy(self.like)

  def get_share(self, *args, **kwargs):
    return copy.deepcopy(self.share)

  def get_event(self, *args, **kwargs):
    return copy.deepcopy(self.event)

  def get_rsvp(self, *args, **kwargs):
    return copy.deepcopy(self.rsvp)

  @classmethod
  def clear(cls):
    cls.activities = cls.like = cls.share = cls.event = cls.rsvp = cls.etag = \
      cls.search_results = cls.last_search_query = None

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
      if activities is None:
        raise NotImplementedError()

    return {
      'items': copy.deepcopy(activities),
      'etag': getattr(self, 'etag', None),
    }

  def create(self, obj, include_link=False, ignore_formatting=False):
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
          error_plain='no %s url to reply to' % self.DOMAIN,
          error_html='no %s url to reply to' % self.DOMAIN)

    content = self._content_for_create(obj, ignore_formatting=ignore_formatting)
    if include_link:
        content += ' - %s' % obj['url']
    ret = {'id': 'fake id', 'url': 'http://fake/url', 'content': content}
    if verb == 'rsvp-yes':
      ret['type'] = 'post'
    return gr_source.creation_result(ret)

  def preview_create(self, obj, include_link=False, ignore_formatting=False):
    if obj.get('verb') == 'like':
      return gr_source.creation_result(
        abort=True, error_plain='Cannot publish likes',
        error_html='Cannot publish likes')

    content = self._content_for_create(obj, ignore_formatting=ignore_formatting)
    if include_link:
        content += ' - %s' % obj['url']

    content = 'preview of ' + content
    return gr_source.creation_result(description=content)


class FakeSource(Source):
  GR_CLASS = FakeGrSource
  SHORT_NAME = 'fake'
  TYPE_LABELS = {'post': 'FakeSource post label'}
  RATE_LIMITED_POLL = datetime.timedelta(hours=30)

  string_id_counter = 1
  gr_source = FakeGrSource()
  username = ndb.StringProperty()

  def silo_url(self):
    return 'http://fa.ke/profile/url'

  def feed_url(self):
    return 'fake feed url'

  def poll_period(self):
    return (self.RATE_LIMITED_POLL if self.rate_limited
            else super(FakeSource, self).poll_period())

  @classmethod
  def new(cls, handler, **props):
    id = None
    if 'url' not in props:
      props['url'] = 'http://fake/url'
    auth_entity = props.get('auth_entity')
    if auth_entity:
      props['auth_entity'] = auth_entity.key
      if auth_entity.user_json:
        user_obj = json.loads(auth_entity.user_json)
        if 'name' not in props:
          props['name'] = user_obj.get('name')
        id = user_obj.get('id')
    if not props.get('name'):
      props['name'] = 'fake'
    if not id:
      id = str(cls.string_id_counter)
      cls.string_id_counter += 1
    return cls(id=id, **props)


class HandlerTest(gr_testutil.TestCase):
  """Base test class.
  """
  def setUp(self):
    super(HandlerTest, self).setUp()
    self.handler = util.Handler(self.request, self.response)
    FakeGrSource.clear()
    util.now_fn = lambda: NOW

    # we use global queries in tests to verify entities in the datastore, so
    # make the datastore stub always return consistent data. not ideal, since it
    # doesn't simulate eventual consistency, but oh well.
    # https://cloud.google.com/appengine/docs/python/tools/localunittesting#Python_Writing_High_Replication_Datastore_tests
    policy = datastore_stub_util.PseudoRandomHRConsistencyPolicy(probability=1)
    self.testbed.init_datastore_v3_stub(consistency_policy=policy)

    # add FakeSource everywhere necessary
    util.BLACKLIST.add('fa.ke')

  def expect_requests_get(self, *args, **kwargs):
    kwargs.setdefault('headers', {}).update(util.USER_AGENT_HEADER)

    if 'stream' not in kwargs:
      kwargs['stream'] = True
    elif kwargs['stream'] == None:
      del kwargs['stream']

    return super(HandlerTest, self).expect_requests_get(*args, **kwargs)

  def expect_webmention_requests_get(self, *args, **kwargs):
    kwargs.setdefault('headers', {}).update(util.USER_AGENT_HEADER)
    return super(HandlerTest, self).expect_requests_get(*args, **kwargs)

  def expect_requests_post(self, *args, **kwargs):
    kwargs.setdefault('headers', {}).update(util.USER_AGENT_HEADER)
    return super(HandlerTest, self).expect_requests_post(*args, **kwargs)

  def expect_requests_head(self, *args, **kwargs):
    kwargs.setdefault('headers', {}).update(util.USER_AGENT_HEADER)
    return super(HandlerTest, self).expect_requests_head(*args, **kwargs)


class ModelsTest(HandlerTest):
  """Sets up some test sources and responses.

  Attributes:
    sources: list of FakeSource
    responses: list of unsaved Response
    taskqueue_stub: the app engine task queue api proxy stub
  """

  def setUp(self):
    super(ModelsTest, self).setUp()

    auth_entities = [
      FakeAuthEntity(
        key=ndb.Key('FakeAuthEntity', '01122334455'),
        user_json=json.dumps({
          'id': '0123456789',
          'name': 'Fake User',
          'url': 'http://fakeuser.com/',
        })),
      FakeAuthEntity(
        key=ndb.Key('FakeAuthEntity', '0022446688'),
        user_json=json.dumps({
          'id': '0022446688',
          'name': 'Another Fake',
          'url': 'http://anotherfake.com/',
        }))
    ]
    for entity in auth_entities:
      entity.put()

    self.sources = [
      FakeSource.new(None, auth_entity=auth_entities[0]),
      FakeSource.new(None, auth_entity=auth_entities[1])]
    for entity in self.sources:
      entity.features = ['listen']
      entity.put()

    self.activities = [{
      'id': 'tag:source.com,2013:%s' % id,
      'url': 'http://source/post/url',
      'object': {
        'objectType': 'note',
        'id': 'tag:source.com,2013:%s' % id,
        'url': 'http://source/post/url',
        'content': 'foo http://target1/post/url bar',
        'to': [{'objectType':'group', 'alias':'@public'}],
        'replies': {
          'items': [{
              'objectType': 'comment',
              'id': 'tag:source.com,2013:1_2_%s' % id,
              'url': 'http://source/comment/url',
              'content': 'foo bar',
              }],
          'totalItems': 1,
          },
        'tags': [{
              'objectType': 'activity',
              'verb': 'like',
              'id': 'tag:source.com,2013:%s_liked_by_alice' % id,
              'object': {'url': 'http://example.com/abc'},
              'author': {'url': 'http://example.com/alice'},
              }, {
              'id': 'tag:source.com,2013:%s_reposted_by_bob' % id,
              'objectType': 'activity',
              'verb': 'share',
              'object': {'url': 'http://example.com/def'},
              'author': {'url': 'http://example.com/bob'},
              }],
        },
      } for id in ('a', 'b', 'c')]
    FakeGrSource.activities = self.activities

    self.responses = []
    created = datetime.datetime.utcnow() - datetime.timedelta(days=10)

    for activity in self.activities:
      obj = activity['object']
      pruned_activity = {
        'id': activity['id'],
        'url': 'http://source/post/url',
        'object': {
          'content': 'foo http://target1/post/url bar',
          }
        }

      comment = obj['replies']['items'][0]
      self.responses.append(Response(
          id=comment['id'],
          activities_json=[json.dumps(pruned_activity)],
          response_json=json.dumps(comment),
          type='comment',
          source=self.sources[0].key,
          unsent=['http://target1/post/url'],
          created=created))

      created += datetime.timedelta(hours=1)

      like = obj['tags'][0]
      self.responses.append(Response(
          id=like['id'],
          activities_json=[json.dumps(pruned_activity)],
          response_json=json.dumps(like),
          type='like',
          source=self.sources[0].key,
          unsent=['http://target1/post/url'],
          created=created))

      created += datetime.timedelta(hours=1)

      share = obj['tags'][1]
      self.responses.append(Response(
          id=share['id'],
          activities_json=[json.dumps(pruned_activity)],
          response_json=json.dumps(share),
          type='repost',
          source=self.sources[0].key,
          unsent=['http://target1/post/url'],
          created=created))

      created += datetime.timedelta(hours=1)


class OAuthStartHandler(oauth_handlers.StartHandler):
  """Stand-in for the oauth-dropins StartHandler, redirects to
  a made-up silo url
  """
  def redirect_url(self, state=None):
    logging.debug('oauth handler redirect')
    return 'http://fake/auth/url?' + urllib.urlencode({
      'redirect_uri': self.to_url(state),
    })


FakeStartHandler = util.oauth_starter(OAuthStartHandler).to('/fakesource/add')


class FakeAddHandler(util.Handler):
  """Handles the authorization callback when handling a fake source
  """
  auth_entity = FakeAuthEntity(user_json=json.dumps({
    'id': '0123456789',
    'name': 'Fake User',
    'url': 'http://fakeuser.com/',
  }))

  @staticmethod
  def with_auth(auth):
    class HandlerWithAuth(FakeAddHandler):
      auth_entity = auth
    return HandlerWithAuth

  def get(self):
    self.maybe_add_or_delete_source(FakeSource, self.auth_entity,
                                    self.request.get('state'))
