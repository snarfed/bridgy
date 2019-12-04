# coding=utf-8
"""Unit test utilities.
"""
from __future__ import unicode_literals

from future.utils import native_str
from future import standard_library
standard_library.install_aliases()
from builtins import str
import copy
import datetime
import logging
import re
import urllib.request, urllib.parse, urllib.error

import appengine_config

from granary import source as gr_source
from google.cloud import ndb
from models import BlogPost, Publish, PublishedPage, Response, Source
from oauth_dropins import handlers as oauth_handlers
from oauth_dropins.models import BaseAuth
from oauth_dropins.webutil.testutil import HandlerTest, get_task_eta, get_task_params
from oauth_dropins.webutil.util import json_dumps, json_loads
import requests

import util

NOW = datetime.datetime.utcnow()


class FakeAuthEntity(BaseAuth):
  user_json = ndb.TextProperty()


class FakeGrSource(gr_source.Source):
  """Fake granary source class.

  Attributes:
    activities, like, reaction, share, event, rsvp, etag, search_results,
    last_search_query, blocked_ids
  """
  NAME = 'FakeSource'
  DOMAIN = 'fa.ke'

  last_search_query = None
  search_results = []

  def user_url(self, id):
    return 'http://fa.ke/' + id

  def user_to_actor(self, user):
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

  @classmethod
  def clear(cls):
    cls.activities = cls.like = cls.reaction = cls.share = cls.event = \
      cls.rsvp = cls.etag = cls.last_search_query = None
    cls.search_results = cls.blocklist_ids = []

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
      obj = activity['object']
      obj['tags'] = [tag for tag in obj.get('tags', []) if
                     'verb' not in tag or
                     (tag['verb'] == 'like' and fetch_likes) or
                     (tag['verb'] == 'share' and fetch_shares) or
                     (tag['verb'] == 'mention' and fetch_mentions) or
                     tag['verb'] == 'react']
      if 'replies' in obj and not fetch_replies:
        obj['replies']['items'] = []

    return {
      'items': activities,
      'etag': getattr(self, 'etag', None),
    }

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
          error_plain='no %s url to reply to' % self.DOMAIN,
          error_html='no %s url to reply to' % self.DOMAIN)

    content = self._content_for_create(obj, ignore_formatting=ignore_formatting)
    if include_link == gr_source.INCLUDE_LINK:
        content += ' - %s' % obj['url']
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
        content += ' - %s' % obj['url']

    content = 'preview of ' + content

    images = self._images(obj)
    if images:
      content += ' with images %s' % ','.join(images)

    return gr_source.creation_result(description=content)

  def delete(self, id):
    return gr_source.creation_result({
      'url': 'http://fake/url',
      'msg': 'delete %s' % id,
    })

  def preview_delete(self, id):
    return gr_source.creation_result(description='delete %s' % id)

  @staticmethod
  def _images(obj):
    return [image['url'] for image in util.get_list(obj, 'image')]


class OAuthStartHandler(oauth_handlers.StartHandler):
  """Stand-in for the oauth-dropins StartHandler, redirects to
  a made-up silo url.
  """
  def redirect_url(self, state=None):
    logging.debug('oauth handler redirect')
    return 'http://fake/auth/url?' + urllib.parse.urlencode({
      'redirect_uri': self.to_url(state),
    })


class FakeSource(Source):
  GR_CLASS = FakeGrSource
  OAUTH_START_HANDLER = OAuthStartHandler
  SHORT_NAME = 'fake'
  TYPE_LABELS = {'post': 'FakeSource post label'}
  RATE_LIMITED_POLL = datetime.timedelta(hours=30)
  URL_CANONICALIZER = util.UrlCanonicalizer(
    domain=GR_CLASS.DOMAIN,
    headers=util.REQUEST_HEADERS)
  PATH_BLACKLIST = (re.compile('^/blacklisted/.*'),)
  HAS_BLOCKS = True

  string_id_counter = 1
  gr_source = FakeGrSource()
  username = ndb.StringProperty()
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
  def new(cls, handler, **props):
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
      id = str(cls.string_id_counter)
      cls.string_id_counter += 1
    return cls(id=native_str(id), **props)

  def put(self, **kwargs):
    self.is_saved = True
    return super(FakeSource, self).put(**kwargs)


class FakeBlogSource(FakeSource):
  SHORT_NAME = 'fake_blog'


class HandlerTest(testutil.HandlerTest):
  """Base test class.
  """
  def setUp(self):
    super(HandlerTest, self).setUp()
    self.handler = util.Handler(self.request, self.response)
    FakeGrSource.clear()
    util.now_fn = lambda: NOW

    # add FakeSource everywhere necessary
    util.BLACKLIST.add('fa.ke')

    util.webmention_endpoint_cache.clear()

  def expect_requests_get(self, *args, **kwargs):
    if 'headers' not in kwargs:
      kwargs['headers'] = util.REQUEST_HEADERS

    return super(HandlerTest, self).expect_requests_get(*args, **kwargs)

  def expect_webmention_requests_get(self, *args, **kwargs):
    kwargs.setdefault('headers', {}).update(util.REQUEST_HEADERS)
    return super(HandlerTest, self).expect_requests_get(
      *args, stream=None, verify=False, **kwargs)

  def expect_requests_post(self, *args, **kwargs):
    kwargs.setdefault('headers', {}).update(util.REQUEST_HEADERS)
    return super(HandlerTest, self).expect_requests_post(*args, **kwargs)

  def expect_requests_head(self, *args, **kwargs):
    kwargs.setdefault('headers', {}).update(util.REQUEST_HEADERS)
    return super(HandlerTest, self).expect_requests_head(*args, **kwargs)


class ModelsTest(HandlerTest):
  """Sets up some test sources and responses.

  Attributes:
    sources: list of FakeSource
    responses: list of unsaved Response
    publishes: list of one unsaved Publish
    blogposts: list of one unsaved BlogPost
  """

  def setUp(self):
    super(ModelsTest, self).setUp()

    # sources
    auth_entities = [
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
    for entity in auth_entities:
      entity.put()

    self.sources = [
      FakeSource.new(None, auth_entity=auth_entities[0]),
      FakeSource.new(None, auth_entity=auth_entities[1])]
    for entity in self.sources:
      entity.features = ['listen']
      entity.put()

    # activities
    self.activities = [{
      'id': 'tag:source.com,2013:%s' % id,
      'url': 'http://fa.ke/post/url',
      'object': {
        'objectType': 'note',
        'id': 'tag:source.com,2013:%s' % id,
        'url': 'http://fa.ke/post/url',
        'content': 'foo http://target1/post/url bar',
        'to': [{'objectType':'group', 'alias':'@public'}],
        'replies': {
          'items': [{
            'objectType': 'comment',
            'id': 'tag:source.com,2013:1_2_%s' % id,
            'url': 'http://fa.ke/comment/url',
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
        }, {
          'id': 'tag:source.com,2013:%s_scissors_by_bob' % id,
          'objectType': 'activity',
          'verb': 'react',
          'content': '✁',
          'object': {'url': 'http://example.com/def'},
          'author': {'url': 'http://example.com/bob'},
        }],
      },
    } for id in ('a', 'b', 'c')]
    FakeGrSource.activities = self.activities

    # responses
    self.responses = []
    created = datetime.datetime.utcnow() - datetime.timedelta(days=10)

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
          response_json=json_dumps(comment),
          type='comment',
          source=self.sources[0].key,
          unsent=['http://target1/post/url'],
          created=created))

      created += datetime.timedelta(hours=1)

      like = obj['tags'][0]
      self.responses.append(Response(
          id=like['id'],
          activities_json=[json_dumps(pruned_activity)],
          response_json=json_dumps(like),
          type='like',
          source=self.sources[0].key,
          unsent=['http://target1/post/url'],
          created=created))

      created += datetime.timedelta(hours=1)

      share = obj['tags'][1]
      self.responses.append(Response(
          id=share['id'],
          activities_json=[json_dumps(pruned_activity)],
          response_json=json_dumps(share),
          type='repost',
          source=self.sources[0].key,
          unsent=['http://target1/post/url'],
          created=created))

      created += datetime.timedelta(hours=1)

      reaction = obj['tags'][2]
      self.responses.append(Response(
          id=reaction['id'],
          activities_json=[json_dumps(pruned_activity)],
          response_json=json_dumps(reaction),
          type='react',
          source=self.sources[0].key,
          unsent=['http://target1/post/url'],
          created=created))

      created += datetime.timedelta(hours=1)

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


FakeStartHandler = util.oauth_starter(OAuthStartHandler).to('/fakesource/add')


class FakeAddHandler(util.Handler):
  """Handles the authorization callback when handling a fake source
  """
  auth_entity = FakeAuthEntity(user_json=json_dumps({
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
