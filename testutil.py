"""Unit test utilities.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import base64
import collections
import datetime
import json
import logging
import urlparse

from google.appengine.datastore import datastore_stub_util
from google.appengine.ext import ndb
import requests

from activitystreams import source as as_source
from models import Response, Source
from tasks import Poll, Propagate
import util
from activitystreams.oauth_dropins.models import BaseAuth
from activitystreams.oauth_dropins.webutil import testutil


def get_task_params(task):
  """Parses a task's POST body and returns the query params in a dict.
  """
  params = urlparse.parse_qs(base64.b64decode(task['body']))
  params = dict((key, val[0]) for key, val in params.items())
  return params


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
    if 'url' not in props:
      props['url'] = 'http://fake/url'
    if 'name' not in props:
      props['name'] = 'fake'
    auth_entity = props.get('auth_entity')
    if auth_entity:
      props['auth_entity'] = auth_entity.key
    inst = cls(id=str(cls.string_id_counter), **props)
    cls.string_id_counter += 1
    return inst


class FakeAsSource(FakeBase, as_source.Source):
  NAME = 'FakeSource'
  DOMAIN = 'fa.ke'

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

  def create(self, obj):
    if obj.get('verb') == 'like':
      raise NotImplementedError()

    return {'id': 'fake id', 'url': 'http://fake/url', 'content': obj['content']}


class FakeSource(FakeBase, Source):
  AS_CLASS = FakeAsSource
  SHORT_NAME = 'fake'

  as_source = FakeAsSource()

  def __init__(self, *args, **kwargs):
    super(FakeSource, self).__init__(*args, **kwargs)
    FakeSource.as_source.put()

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

  def get_comment(self, comment_id, activity_id=None):
    comment = self._get('comment')
    return comment if comment else super(FakeSource, self).get_comment(comment_id)


class HandlerTest(testutil.HandlerTest):
  """Base test class.
  """
  def setUp(self):
    super(HandlerTest, self).setUp()
    self.handler = util.Handler(self.request, self.response)
    # TODO: remove this and don't depend on consistent global queries
    self.testbed.init_datastore_v3_stub(consistency_policy=None)

    # don't make actual HTTP requests to follow original post url redirects
    def fake_head(url, **kwargs):
      resp = requests.Response()
      resp.url = url
      resp.headers['content-type'] = 'text/html; charset=UTF-8'
      return resp
    self.mox.stubs.Set(requests, 'head', fake_head)


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
      entity.put()

    self.activities = [{
      'id': 'tag:source.com,2013:%s' % id,
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
    for activity in self.activities:
      obj = activity['object']
      for response_obj in obj['replies']['items'] + obj['tags']:
        self.responses.append(Response(id=response_obj['id'],
                                       activity_json=json.dumps(activity),
                                       response_json=json.dumps(response_obj),
                                       source=self.sources[0].key,
                                       unsent=['http://target1/post/url']))
