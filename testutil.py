"""Unit test utilities.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import base64
import collections
import datetime
import json
import logging
import urlparse
import webapp2

import appengine_config

from google.appengine.datastore import datastore_stub_util
from google.appengine.ext import ndb
import requests

from activitystreams import source as as_source
from models import Response, Source
import util
from activitystreams import testutil as as_testutil
from activitystreams.oauth_dropins.models import BaseAuth

# mirror some methods from webutil.testutil
from activitystreams.oauth_dropins.webutil.testutil import get_task_eta
from activitystreams.oauth_dropins.webutil.testutil import get_task_params
from activitystreams.oauth_dropins import handlers as oauth_handlers


class FakeAuthEntity(BaseAuth):
  user_json = ndb.TextProperty()


class FakeBase(ndb.Model):
  """Not thread safe.
  """

  string_id_counter = 1
  # class attr. maps (string source key, type name) to object or list.
  # can't use instance attrs because code fetches FakeSource instances from the
  # datastore.
  data = {}

  def _set(self, name, val):
    FakeBase.data[(self.key.urlsafe(), name)] = val

  def _get(self, name):
    return FakeBase.data.get((self.key.urlsafe(), name))

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


class FakeAsSource(FakeBase, as_source.Source):
  NAME = 'FakeSource'
  DOMAIN = 'fa.ke'

  def user_url(self, id):
    return 'http://fa.ke/' + id

  def set_like(self, val):
    self._set('like', val)

  def get_like(self, activity_user_id, activity_id, like_user_id):
    got = self._get('like')
    return got

  def set_share(self, val):
    self._set('repost', val)

  def get_share(self, activity_user_id, activity_id, repost_user_id):
    return self._get('repost')

  def set_rsvp(self, val):
    self._set('rsvp', val)

  def get_rsvp(self, activity_user_id, event_id, rsvp_user_id):
    return self._get('rsvp')

  def user_to_actor(self, user):
    return user

  def create(self, obj, include_link=False):
    verb = obj.get('verb')
    type = obj.get('objectType')
    if verb == 'like':
      return as_source.creation_result(
        abort=True, error_plain='Cannot publish likes',
        error_html='Cannot publish likes')
    if 'content' not in obj:
      return as_source.creation_result(
        abort=False, error_plain='No content',
        error_html='No content')

    if type == 'comment':
      base_url = self.base_object(obj).get('url')
      if not base_url:
        return as_source.creation_result(
          abort=True,
          error_plain='no %s url to reply to' % self.DOMAIN,
          error_html='no %s url to reply to' % self.DOMAIN)

    content = obj['content'] + (' - %s' % obj['url'] if include_link else '')
    ret = {'id': 'fake id', 'url': 'http://fake/url', 'content': content}
    if verb == 'rsvp-yes':
      ret['type'] = 'post'
    return as_source.creation_result(ret)

  def preview_create(self, obj, include_link=False):
    if obj.get('verb') == 'like':
      return as_source.creation_result(
        abort=True, error_plain='Cannot publish likes',
        error_html='Cannot publish likes')
    content = 'preview of ' + obj['content'] + (' - %s' % obj['url']
                                                if include_link else '')
    return as_source.creation_result(
      content=content, description='FakeAsSource preview description')


class FakeSource(FakeBase, Source):
  AS_CLASS = FakeAsSource
  SHORT_NAME = 'fake'
  TYPE_LABELS = {'post': 'FakeSource post label'}

  as_source = FakeAsSource()

  def __init__(self, *args, **kwargs):
    super(FakeSource, self).__init__(*args, **kwargs)
    ndb.transaction(FakeSource.as_source.put,
                    propagation=ndb.TransactionOptions.INDEPENDENT)

  def silo_url(self):
    return 'http://fa.ke/profile/url'

  def set_activities(self, val):
    self._set('activities', val)

  def get_activities_response(self, fetch_replies=False, fetch_likes=False,
                              fetch_shares=False, count=None, etag=None,
                              min_id=None, cache=None):
    return {'items': self._get('activities'), 'etag': self._get('etag')}

  def get_post(self, id):
    return self.get_activities()[int(id)]

  def set_comment(self, val):
    self._set('comment', val)

  def get_comment(self, comment_id, activity_id=None, activity_author_id=None):
    comment = self._get('comment')
    return comment if comment else super(FakeSource, self).get_comment(comment_id)

  def feed_url(self):
    return 'fake feed url'


class HandlerTest(as_testutil.TestCase):
  """Base test class.
  """
  def setUp(self):
    super(HandlerTest, self).setUp()
    self.handler = util.Handler(self.request, self.response)
    logging.getLogger().removeHandler(appengine_config.ereporter_logging_handler)
    # TODO: remove this and don't depend on consistent global queries
    self.testbed.init_datastore_v3_stub(consistency_policy=None)
    util.WEBMENTION_BLACKLIST.add('fa.ke')  # for FakeSource

    # don't make actual HTTP requests to follow original post url redirects
    def fake_head(url, **kwargs):
      resp = requests.Response()
      resp.url = url
      if '.' in url or url.startswith('http'):
        resp.headers['content-type'] = 'text/html; charset=UTF-8'
        resp.status_code = 200
      else:
        resp.status_code = 404
      return resp
    self.mox.stubs.Set(requests, 'head', fake_head)

    self._is_head_mocked = False  # expect_requests_head() sets this to True

  def expect_requests_head(self, *args, **kwargs):
    if not self._is_head_mocked:
      self.mox.StubOutWithMock(requests, 'head', use_mock_anything=True)
      self._is_head_mocked = True
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

    self.sources = [FakeSource.new(None), FakeSource.new(None)]
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
    self.sources[0].set_activities(self.activities)

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
    return 'http://fake/auth/url'


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
