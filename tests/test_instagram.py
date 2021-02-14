"""Unit tests for instagram.py.
"""
import copy
import urllib.request, urllib.parse, urllib.error

import appengine_config  # injects 2013 into tag URIs in test_instagram objects

from granary import instagram as gr_instagram
from granary.tests.test_instagram import (
  HTML_FEED_COMPLETE,
  HTML_FOOTER,
  HTML_HEADER,
  HTML_PHOTO_ACTIVITY,
  HTML_PHOTO_ACTIVITY_LIKES,
  HTML_PHOTO_LIKES_RESPONSE,
  HTML_PROFILE_COMPLETE,
  HTML_PROFILE_PRIVATE_COMPLETE,
  HTML_VIDEO_ACTIVITY,
  HTML_VIDEO_ACTIVITY_FULL,
  HTML_VIDEO_EXTRA_COMMENT_OBJ,
  HTML_VIDEO_PAGE,
  HTML_VIEWER_CONFIG,
  LIKE_OBJS,
)
from oauth_dropins.webutil.util import HTTP_TIMEOUT, json_dumps, json_loads

import app
from instagram import Instagram
from models import Activity, Domain
from .testutil import ModelsTest

HTML_VIDEO_WITH_VIEWER = copy.deepcopy(HTML_VIDEO_PAGE)
HTML_VIDEO_WITH_VIEWER['config'] = HTML_VIEWER_CONFIG
HTML_VIDEO_COMPLETE = HTML_HEADER + json_dumps(HTML_VIDEO_WITH_VIEWER) + HTML_FOOTER


class InstagramTest(ModelsTest):

  def setUp(self):
    super(InstagramTest, self).setUp()
    self.source = Instagram.new(self.handler, actor=self.actor)
    self.domain = Domain(id='snarfed.org', tokens=['towkin']).put()
    self.auth = f'token=towkin&key={self.source.key.urlsafe().decode()}'

  def get_response(self, path_query, auth=True, **kwargs):
    if auth and '?' not in path_query:
      path_query += f'?{self.auth}'
    return app.application.get_response(f'/instagram/browser/{path_query}',
                                        method='POST', **kwargs)

  def store_activity(self):
    activity = copy.deepcopy(HTML_PHOTO_ACTIVITY)
    activity['actor']['url'] = 'http://snarfed.org/'
    return Activity(id='tag:instagram.com,2013:123_456', source=self.source.key,
                    activity_json=json_dumps(activity)).put()

  def test_new(self):
    self.assertIsNone(self.source.auth_entity)
    self.assertEqual('snarfed', self.source.key.string_id())
    self.assertEqual('http://pic.ture/url', self.source.picture)
    self.assertEqual('https://www.instagram.com/snarfed/', self.source.silo_url())
    self.assertEqual('Ryan B', self.source.name)
    self.assertEqual('snarfed (Instagram)', self.source.label())

  def test_canonicalize_url(self):
    self.unstub_requests_head()
    for url in (
        'http://www.instagram.com/p/abcd',
        'https://www.instagram.com/p/abcd',
        'https://www.instagram.com/p/abcd/',
        'https://instagram.com/p/abcd',
    ):
      self.assertEqual('https://www.instagram.com/p/abcd/',
                       self.source.canonicalize_url(url))

    self.assertIsNone(self.source.canonicalize_url('https://www.foo.com/p/abcd/'))

  def test_canonicalize_url_approve_checks_full_url(self):
    """...specifically, that the regex ends with a $
    https://github.com/snarfed/bridgy/issues/686
    """
    self.assertEqual('https://www.instagram.com/p/abcd/123/',
                     self.source.canonicalize_url('https://www.instagram.com/p/abcd/123'))

  def test_get_activities_response_activity_id(self):
    Activity(id='tag:instagram.com,2013:123',
             activity_json=json_dumps({'foo': 'bar'})).put()

    resp = self.source.get_activities_response(activity_id='123')
    self.assertEqual([{'foo': 'bar'}], resp['items'])

  def test_get_activities_response_no_activity_id(self):
    Activity(id='tag:instagram.com,2013:123', source=self.source.key,
             activity_json=json_dumps({'foo': 'bar'})).put()
    Activity(id='tag:instagram.com,2013:456', source=self.source.key,
             activity_json=json_dumps({'baz': 'biff'})).put()

    other = Instagram.new(self.handler, actor={'username': 'other'}).put()
    Activity(id='tag:instagram.com,2013:789', source=other,
             activity_json=json_dumps({'boo': 'bah'})).put()

    resp = self.source.get_activities_response()
    self.assert_equals([{'foo': 'bar'}, {'baz': 'biff'}], resp['items'])

  def test_get_activities_response_no_stored_activity(self):
    resp = self.source.get_activities_response(activity_id='123')
    self.assertEqual([], resp['items'])

  def test_get_comment(self):
    self.assert_equals(
      HTML_VIDEO_EXTRA_COMMENT_OBJ,
      self.source.get_comment('020', activity=HTML_VIDEO_ACTIVITY_FULL))

  def test_get_comment_no_matching_id(self):
    self.assertIsNone(self.source.get_comment('333', activity=HTML_VIDEO_ACTIVITY_FULL))

  def test_get_comment_no_activity_kwarg(self):
    self.assertIsNone(self.source.get_comment('020'))

  def test_get_like(self):
    self.assert_equals(LIKE_OBJS[1], self.source.get_like(
      'unused', '123', '9', activity=HTML_PHOTO_ACTIVITY_LIKES))

  def test_get_like_no_matching_user(self):
    self.assertIsNone(self.source.get_like(
      'unused', '123', '222', activity=HTML_PHOTO_ACTIVITY_LIKES))

  def test_get_like_no_activity_kwarg(self):
    self.assertIsNone(self.source.get_like('unused', '123', '9'))

  def test_homepage(self):
    resp = self.get_response('homepage', text=HTML_FEED_COMPLETE)
    self.assertEqual(200, resp.status_int)
    self.assertEqual('snarfed', resp.json)

  def test_homepage_bad_html(self):
    resp = self.get_response('homepage', text='not a logged in IG feed')
    self.assertEqual(400, resp.status_int)
    self.assertIn("Couldn't determine logged in Instagram user", resp.text)

  def test_profile_new_user(self):
    self.assertIsNone(Instagram.get_by_id('snarfed'))

    self.expect_webmention_requests_get('https://snarfed.org/', '')
    self.mox.ReplayAll()

    resp = self.get_response('profile?token=towkin', text=HTML_PROFILE_COMPLETE)

    self.assertEqual(200, resp.status_int)
    self.assertEqual(self.source.key.urlsafe().decode(), resp.json)

    ig = Instagram.get_by_id('snarfed')
    self.assertEqual('Ryan B', ig.name)
    self.assertEqual('https://scontent-sjc2-1.cdninstagram.com/hphotos-xfa1/t51.2885-19/11373714_959073410822287_2004790583_a.jpg', ig.picture)
    self.assertEqual('https://www.instagram.com/snarfed/', ig.silo_url())
    self.assertEqual(['https://snarfed.org/'], ig.domain_urls)
    self.assertEqual(['snarfed.org'], ig.domains)

  def test_profile_private_account(self):
    resp = self.get_response('profile', text=HTML_PROFILE_PRIVATE_COMPLETE)
    self.assertEqual(400, resp.status_int)
    self.assertIn('Your Instagram account is private.', resp.text)

  def test_post(self):
    self.source.put()

    resp = self.get_response('post', text=HTML_VIDEO_COMPLETE)
    self.assertEqual(200, resp.status_int, resp.text)
    self.assertEqual(HTML_VIDEO_ACTIVITY_FULL, resp.json)

    activities = Activity.query().fetch()
    self.assertEqual(1, len(activities))
    self.assertEqual(self.source.key, activities[0].source)
    self.assertEqual(HTML_VIDEO_ACTIVITY_FULL, json_loads(activities[0].activity_json))

  def test_post_empty(self):
    self.source.put()
    empty = HTML_HEADER + json_dumps({'config': HTML_VIEWER_CONFIG}) + HTML_FOOTER
    resp = self.get_response('post', text=empty)
    self.assertEqual(400, resp.status_int)
    self.assertIn('No Instagram post found in HTML', resp.text)

  def test_post_merge_comments(self):
    self.source.put()

    # existing activity with one of the two comments in HTML_VIDEO_COMPLETE
    existing_activity = copy.deepcopy(HTML_VIDEO_ACTIVITY)
    existing_activity['object']['replies'] = {
      'totalItems': 1,
      'items': [HTML_VIDEO_ACTIVITY_FULL['object']['replies']['items'][0]],
    }
    activity_key = Activity(id='tag:instagram.com,2013:789_456',
                            activity_json=json_dumps(existing_activity)).put()

    # send HTML_VIDEO_COMPLETE to /post, check that the response and stored
    # activity have both of its comments
    resp = self.get_response('post', text=HTML_VIDEO_COMPLETE)
    self.assertEqual(200, resp.status_int, resp.text)
    self.assert_equals(HTML_VIDEO_ACTIVITY_FULL, resp.json)

    activity = activity_key.get()
    self.assert_equals(HTML_VIDEO_ACTIVITY_FULL, json_loads(activity.activity_json))

  def test_likes(self):
    self.source.put()
    key = self.store_activity()

    resp = self.get_response(f'likes?id=tag:instagram.com,2013:123_456&{self.auth}',
                             text=json_dumps(HTML_PHOTO_LIKES_RESPONSE))
    self.assertEqual(200, resp.status_int, resp.text)
    self.assertEqual(LIKE_OBJS, resp.json)

    activity = json_loads(key.get().activity_json)
    self.assertEqual(LIKE_OBJS, activity['object']['tags'])

  def test_poll(self):
    key = self.source.put()
    self.expect_task('poll', eta_seconds=0, source_key=key,
                     last_polled='1970-01-01-00-00-00')
    self.mox.ReplayAll()

    resp = self.get_response(f'poll')
    self.assertEqual(200, resp.status_int, resp.text)
    self.assertEqual('OK', resp.json)
