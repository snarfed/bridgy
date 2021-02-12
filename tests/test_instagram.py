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
    self.ig = Instagram.new(self.handler, actor=self.actor)
    self.domain = Domain(id='snarfed.org', tokens=['towkin']).put()

  def store_activity(self):
    activity = copy.deepcopy(HTML_PHOTO_ACTIVITY)
    activity['actor']['url'] = 'http://snarfed.org/'
    return Activity(id='tag:instagram.com,2013:123_456',
                    activity_json=json_dumps(activity)).put()

  def test_new(self):
    self.assertIsNone(self.ig.auth_entity)
    self.assertEqual('snarfed', self.ig.key.string_id())
    self.assertEqual('http://pic.ture/url', self.ig.picture)
    self.assertEqual('https://www.instagram.com/snarfed/', self.ig.silo_url())
    self.assertEqual('Ryan B', self.ig.name)
    self.assertEqual('snarfed (Instagram)', self.ig.label())

  def test_canonicalize_url(self):
    self.unstub_requests_head()
    for url in (
        'http://www.instagram.com/p/abcd',
        'https://www.instagram.com/p/abcd',
        'https://www.instagram.com/p/abcd/',
        'https://instagram.com/p/abcd',
    ):
      self.assertEqual('https://www.instagram.com/p/abcd/',
                       self.ig.canonicalize_url(url))

    self.assertIsNone(self.ig.canonicalize_url('https://www.foo.com/p/abcd/'))

  def test_canonicalize_url_approve_checks_full_url(self):
    """...specifically, that the regex ends with a $
    https://github.com/snarfed/bridgy/issues/686
    """
    self.assertEqual('https://www.instagram.com/p/abcd/123/',
                     self.ig.canonicalize_url('https://www.instagram.com/p/abcd/123'))

  def test_get_activities_response_activity_id(self):
    Activity(id='tag:instagram.com,2013:123',
             activity_json=json_dumps({'foo': 'bar'})).put()

    resp = self.ig.get_activities_response(activity_id='123')
    self.assertEqual([{'foo': 'bar'}], resp['items'])

  def test_get_activities_response_no_activity_id(self):
    Activity(id='tag:instagram.com,2013:123', source=self.ig.key,
             activity_json=json_dumps({'foo': 'bar'})).put()
    Activity(id='tag:instagram.com,2013:456', source=self.ig.key,
             activity_json=json_dumps({'baz': 'biff'})).put()

    other = Instagram.new(self.handler, actor={'username': 'other'}).put()
    Activity(id='tag:instagram.com,2013:789', source=other,
             activity_json=json_dumps({'boo': 'bah'})).put()

    resp = self.ig.get_activities_response()
    self.assert_equals([{'foo': 'bar'}, {'baz': 'biff'}], resp['items'])

  def test_get_activities_response_no_stored_activity(self):
    resp = self.ig.get_activities_response(activity_id='123')
    self.assertEqual([], resp['items'])

  def test_get_comment(self):
    self.assert_equals(
      HTML_VIDEO_EXTRA_COMMENT_OBJ,
      self.ig.get_comment('020', activity=HTML_VIDEO_ACTIVITY_FULL))

  def test_get_comment_no_matching_id(self):
    self.assertIsNone(self.ig.get_comment('333', activity=HTML_VIDEO_ACTIVITY_FULL))

  def test_get_comment_no_activity_kwarg(self):
    self.assertIsNone(self.ig.get_comment('020'))

  def test_get_like(self):
    self.assert_equals(LIKE_OBJS[1], self.ig.get_like(
      'unused', '123', '9', activity=HTML_PHOTO_ACTIVITY_LIKES))

  def test_get_like_no_matching_user(self):
    self.assertIsNone(self.ig.get_like(
      'unused', '123', '222', activity=HTML_PHOTO_ACTIVITY_LIKES))

  def test_get_like_no_activity_kwarg(self):
    self.assertIsNone(self.ig.get_like('unused', '123', '9'))

  def test_homepage(self):
    resp = app.application.get_response(
      '/instagram/browser/homepage', method='POST', text=HTML_FEED_COMPLETE)
    self.assertEqual(200, resp.status_int)
    self.assertEqual('snarfed', resp.json)

  def test_homepage_bad_html(self):
    resp = app.application.get_response(
      '/instagram/browser/homepage', method='POST',
      text='not a logged in IG feed')
    self.assertEqual(400, resp.status_int)
    self.assertIn("Couldn't determine logged in Instagram user", resp.text)

  def test_profile_new_user(self):
    self.assertIsNone(Instagram.get_by_id('snarfed'))

    self.expect_webmention_requests_get('https://snarfed.org/', '')
    self.mox.ReplayAll()

    resp = app.application.get_response(
      '/instagram/browser/profile?token=towkin', method='POST',
      text=HTML_PROFILE_COMPLETE)

    self.assertEqual(200, resp.status_int)
    self.assertEqual(self.ig.key.urlsafe().decode(), resp.json)

    ig = Instagram.get_by_id('snarfed')
    self.assertEqual('Ryan B', ig.name)
    self.assertEqual('https://scontent-sjc2-1.cdninstagram.com/hphotos-xfa1/t51.2885-19/11373714_959073410822287_2004790583_a.jpg', ig.picture)
    self.assertEqual('https://www.instagram.com/snarfed/', ig.silo_url())
    self.assertEqual(['https://snarfed.org/'], ig.domain_urls)
    self.assertEqual(['snarfed.org'], ig.domains)

  def test_profile_private_account(self):
    resp = app.application.get_response(
      '/instagram/browser/profile?token=towkin', method='POST',
      text=HTML_PROFILE_PRIVATE_COMPLETE)
    self.assertEqual(400, resp.status_int)
    self.assertIn('Your Instagram account is private.', resp.text)

  def test_profile_missing_token(self):
    resp = app.application.get_response(
      '/instagram/browser/profile', method='POST',
      text=HTML_PROFILE_COMPLETE)
    self.assertEqual(400, resp.status_int)
    self.assertIn('Missing required parameter: token', resp.text)

  def test_profile_no_stored_token(self):
    self.domain.delete()
    resp = app.application.get_response(
      '/instagram/browser/profile?token=towkin', method='POST',
      text=HTML_PROFILE_COMPLETE)
    self.assertEqual(403, resp.status_int)
    self.assertIn("towkin is not authorized for any of: {'snarfed.org'}", resp.text)

  def test_profile_bad_token(self):
    resp = app.application.get_response(
      '/instagram/browser/profile?token=nope', method='POST',
      text=HTML_PROFILE_COMPLETE)
    self.assertEqual(403, resp.status_int)
    self.assertIn("nope is not authorized for any of: {'snarfed.org'}", resp.text)

  def test_post(self):
    self.ig.put()

    resp = app.application.get_response(
      '/instagram/browser/post?token=towkin', method='POST',
      text=HTML_VIDEO_COMPLETE)
    self.assertEqual(200, resp.status_int, resp.text)
    self.assertEqual(HTML_VIDEO_ACTIVITY_FULL, resp.json)

    activities = Activity.query().fetch()
    self.assertEqual(1, len(activities))
    self.assertEqual(self.ig.key, activities[0].source)
    self.assertEqual(HTML_VIDEO_ACTIVITY_FULL, json_loads(activities[0].activity_json))

  def test_post_no_source(self):
    resp = app.application.get_response(
      '/instagram/browser/post?token=towkin', method='POST',
      text=HTML_VIDEO_COMPLETE)
    self.assertEqual(404, resp.status_int)
    self.assertIn('No account found for Instagram user snarfed', resp.text)

  def test_post_empty(self):
    empty = HTML_HEADER + json_dumps({'config': HTML_VIEWER_CONFIG}) + HTML_FOOTER
    resp = app.application.get_response(
      '/instagram/browser/post?token=towkin', method='POST', text=empty)
    self.assertEqual(400, resp.status_int)
    self.assertIn('No Instagram post found in HTML', resp.text)

  def test_post_missing_token(self):
    key = self.ig.put()
    resp = app.application.get_response(
      f'/instagram/browser/post?key={key.urlsafe().decode()}',
      method='POST', text=HTML_VIDEO_COMPLETE)
    self.assertEqual(400, resp.status_int)
    self.assertIn('Missing required parameter: token', resp.text)

  def test_post_merge_comments(self):
    self.ig.put()

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
    resp = app.application.get_response(
      '/instagram/browser/post?token=towkin', method='POST',
      text=HTML_VIDEO_COMPLETE)

    self.assertEqual(200, resp.status_int, resp.text)
    self.assert_equals(HTML_VIDEO_ACTIVITY_FULL, resp.json)

    activity = activity_key.get()
    self.assert_equals(HTML_VIDEO_ACTIVITY_FULL, json_loads(activity.activity_json))

  def test_likes(self):
    key = self.store_activity()

    resp = app.application.get_response(
      '/instagram/browser/likes?id=tag:instagram.com,2013:123_456&token=towkin',
      method='POST', text=json_dumps(HTML_PHOTO_LIKES_RESPONSE))

    self.assertEqual(200, resp.status_int, resp.text)
    self.assertEqual(LIKE_OBJS, resp.json)

    activity = json_loads(key.get().activity_json)
    self.assertEqual(LIKE_OBJS, activity['object']['tags'])

  def test_likes_bad_id(self):
    resp = app.application.get_response(
      '/instagram/browser/likes?id=789&token=towkin', method='POST',
      text=json_dumps(HTML_PHOTO_LIKES_RESPONSE))
    self.assertEqual(400, resp.status_int)
    self.assertIn('Expected id to be tag URI', resp.text)

  def test_likes_no_activity(self):
    resp = app.application.get_response(
      '/instagram/browser/likes?id=tag:instagram.com,2013:789&token=towkin',
      method='POST', text=json_dumps(HTML_PHOTO_LIKES_RESPONSE))
    self.assertEqual(404, resp.status_int)
    self.assertIn('No Instagram post found for id tag:instagram.com,2013:789',
                  resp.text)

  def test_likes_bad_token(self):
    self.store_activity()
    resp = app.application.get_response(
      '/instagram/browser/likes?id=tag:instagram.com,2013:123_456&token=nope',
      method='POST', text=json_dumps(HTML_PHOTO_LIKES_RESPONSE))
    self.assertEqual(403, resp.status_int)
    self.assertIn("nope is not authorized for any of: {'snarfed.org'}", resp.text)

  def test_poll(self):
    self.ig.put()
    self.expect_task('poll', eta_seconds=0, source_key=self.ig.key,
                     last_polled='1970-01-01-00-00-00')
    self.mox.ReplayAll()

    resp = app.application.get_response(
      '/instagram/browser/poll?username=snarfed', method='POST')
    self.assertEqual(200, resp.status_int, resp.text)
    self.assertEqual('OK', resp.json)

  def test_poll_no_source(self):
    self.stub_create_task()
    resp = app.application.get_response(
      '/instagram/browser/poll?username=snarfed', method='POST')
    self.assertEqual(404, resp.status_int)
    self.assertIn('No account found for Instagram user snarfed', resp.text)
