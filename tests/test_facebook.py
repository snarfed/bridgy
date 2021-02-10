"""Unit tests for facebook.py.
"""
import copy
from datetime import datetime
import logging

import appengine_config  # injects 2013 into tag URIs in test_facebook objects

from granary import facebook as gr_facebook
from granary.tests.test_facebook import (
  MBASIC_HTML_TIMELINE,
  MBASIC_HTML_POST,
  MBASIC_HTML_REACTIONS,
  MBASIC_HTML_ABOUT,
  MBASIC_ACTOR,
  MBASIC_ABOUT_ACTOR,
  MBASIC_ACTIVITIES,
  MBASIC_ACTIVITIES_REPLIES,
  MBASIC_ACTIVITIES_REPLIES_REACTIONS,
  MBASIC_ACTIVITY,
  MBASIC_REACTION_TAGS,
)
from oauth_dropins.webutil.util import json_dumps, json_loads

import app
from facebook import Facebook
from models import Activity, Domain
from .testutil import ModelsTest


class FacebookTest(ModelsTest):

  def setUp(self):
    super(FacebookTest, self).setUp()
    self.actor['numeric_id'] = '212038'
    self.fb = Facebook.new(self.handler, actor=self.actor)
    self.domain = Domain(id='snarfed.org', tokens=['towkin']).put()
    self.mox.StubOutWithMock(gr_facebook, 'now_fn')

  def store_activity(self):
    activity = copy.deepcopy(MBASIC_ACTIVITIES[0])
    activity['actor']['url'] = 'http://snarfed.org/'
    return Activity(id='tag:facebook.com,2013:123',
                    activity_json=json_dumps(activity)).put()

  def test_canonicalize_url_basic(self):
    for expected, input in (
      ('https://www.facebook.com/212038/posts/314159',
       'https://facebook.com/snarfed/photos.php?fbid=314159'),
      # note. https://github.com/snarfed/bridgy/issues/429
      ('https://www.facebook.com/212038/posts/314159',
       'https://www.facebook.com/notes/ryan-b/title/314159'),
      ('https://www.facebook.com/212038/posts/314159',
       'https://www.facebook.com/photo.php?fbid=314159&set=a.456.2393090.212038&type=1&theater'),
      ('https://www.facebook.com/212038/posts/314159',
       'https://facebook.com/permalink.php?story_fbid=314159&id=212038'),
      ('https://www.facebook.com/212038/posts/314159',
       'https://facebook.com/permalink.php?story_fbid=314159&amp;id=212038'),
      ('https://www.facebook.com/212038/posts/314159',
       'https://m.facebook.com/story.php?id=212038&story_fbid=314159'),
      ):
      self.assertEqual(expected, self.fb.canonicalize_url(input), input)

  def test_canonicalize_url_username(self):
    # we shouldn't touch username when it appears elsewhere in the url
    self.fb.username = 'snarfed'
    self.assertEqual('https://www.facebook.com/25624/posts/snarfed',
                     self.fb.canonicalize_url(
                       'http://www.facebook.com/25624/posts/snarfed'))

    # if no username, fall through
    self.fb.username = None
    self.assertEqual('https://www.facebook.com/212038/posts/444',
                     self.fb.canonicalize_url(
                       'https://www.facebook.com/mr-disguise/posts/444'))

  def test_canonicalize_url_not_facebook(self):
    """Shouldn't try to extract id and fetch post for non-facebook.com URLs."""
    url = 'https://twitter.com/foo/status/123'
    self.assertIsNone(self.fb.canonicalize_url(url))

  def test_profile_new_user(self):
    self.assertIsNone(Facebook.get_by_id('212038'))

    # webmention discovery
    self.expect_requests_get('https://snarfed.org/', '', stream=None, verify=False)
    self.mox.ReplayAll()

    resp = app.application.get_response(
      '/facebook/browser/profile?token=towkin', method='POST', text=MBASIC_HTML_ABOUT)

    self.assertEqual(200, resp.status_int)
    self.assertEqual([], resp.json)

    fb = Facebook.get_by_id('212038')
    self.assertEqual('Ryan Barrett', fb.name)
    self.assertEqual('https://scontent-sjc3-1.xx.fbcdn.net/v/t1.0-1/cp0/e15/q65/p74x74/39610935_10104076860151373_4179282966062563328_o.jpg?...', fb.picture)
    self.assertEqual(['https://snarfed.org/'], fb.domain_urls)
    self.assertEqual(['snarfed.org'], fb.domains)

  def test_profile_missing_token(self):
    resp = app.application.get_response(
      '/facebook/browser/profile', method='POST', text=MBASIC_HTML_ABOUT)
    self.assertEqual(400, resp.status_int)
    self.assertIn('Missing required parameter: token', resp.text)

  def test_profile_no_stored_token(self):
    self.domain.delete()
    resp = app.application.get_response(
      '/facebook/browser/profile?token=towkin', method='POST',
      text=MBASIC_HTML_ABOUT)
    self.assertEqual(403, resp.status_int)
    self.assertIn("towkin is not authorized for any of: {'snarfed.org'}", resp.text)

  def test_profile_bad_token(self):
    resp = app.application.get_response(
      '/facebook/browser/profile?token=nope', method='POST',
      text=MBASIC_HTML_ABOUT)
    self.assertEqual(403, resp.status_int)
    self.assertIn("nope is not authorized for any of: {'snarfed.org'}", resp.text)

    self.assertIsNone(Facebook.get_by_id('212038'))

  def test_feed(self):
    gr_facebook.now_fn().MultipleTimes().AndReturn(datetime(1999, 1, 1))
    self.mox.ReplayAll()

    resp = app.application.get_response(
      '/facebook/browser/feed?token=towkin&key={key.urlsafe().decode()}',
      method='POST', text=MBASIC_HTML_TIMELINE)
    self.assertEqual(200, resp.status_int)
    self.assertEqual(MBASIC_ACTIVITIES, resp.json)

  def test_post(self):
    key = self.fb.put()
    gr_facebook.now_fn().MultipleTimes().AndReturn(datetime(1999, 1, 1))
    self.mox.ReplayAll()

    resp = app.application.get_response(
      f'/facebook/browser/post?token=towkin&key={key.urlsafe().decode()}',
      method='POST', text=MBASIC_HTML_POST)
    self.assertEqual(200, resp.status_int, resp.text)
    self.assertEqual(MBASIC_ACTIVITY, resp.json)

    activities = Activity.query().fetch()
    self.assertEqual(1, len(activities))
    self.assertEqual(self.fb.key, activities[0].source)
    self.assertEqual(MBASIC_ACTIVITY, json_loads(activities[0].activity_json))

  def test_post_no_source(self):
    key = self.fb.put()
    key.delete()
    gr_facebook.now_fn().MultipleTimes().AndReturn(datetime(1999, 1, 1))
    self.mox.ReplayAll()

    resp = app.application.get_response(
      f'/facebook/browser/post?token=towkin&key={key.urlsafe().decode()}',
      method='POST', text=MBASIC_HTML_POST)
    self.assertEqual(400, resp.status_int)
    self.assertIn('Source key not found', resp.text)

  def test_post_empty(self):
    key = self.fb.put()

    resp = app.application.get_response(
      f'/facebook/browser/post?token=towkin&key={key.urlsafe().decode()}',
      method='POST', text="""\
<!DOCTYPE html>
<html><body></body></html>""")
    self.assertEqual(400, resp.status_int)
    self.assertIn('No Facebook post found in HTML', resp.text)

  def test_post_missing_token(self):
    key = self.fb.put()
    gr_facebook.now_fn().MultipleTimes().AndReturn(datetime(1999, 1, 1))
    self.mox.ReplayAll()

    resp = app.application.get_response(
      f'/facebook/browser/post?key={key.urlsafe().decode()}',
      method='POST', text=MBASIC_HTML_POST)
    self.assertEqual(400, resp.status_int)
    self.assertIn('Missing required parameter: token', resp.text)

  def test_post_merge_comments(self):
    key = self.fb.put()
    gr_facebook.now_fn().MultipleTimes().AndReturn(datetime(1999, 1, 1))
    self.mox.ReplayAll()

    # existing activity with one of the two comments in MBASIC_ACTIVITIES
    existing_activity = copy.deepcopy(MBASIC_ACTIVITIES[1])
    existing_activity['object']['replies'] = {
      'totalItems': 1,
      'items': [MBASIC_ACTIVITIES_REPLIES[1]['object']['replies']['items'][0]],
    }
    activity_key = Activity(id='tag:facebook.com,2013:456',
                            activity_json=json_dumps(existing_activity)).put()

    # send MBASIC_HTML_POST to /post, check that the response and stored
    # activity have both of its comments
    resp = app.application.get_response(
      f'/facebook/browser/post?token=towkin&key={key.urlsafe().decode()}',
      method='POST', text=MBASIC_HTML_POST)

    self.assertEqual(200, resp.status_int, resp.text)
    self.assert_equals(MBASIC_ACTIVITY, resp.json)

    activity = activity_key.get()
    self.assert_equals(MBASIC_ACTIVITY, json_loads(activity.activity_json))

  def test_likes(self):
    key = self.store_activity()

    resp = app.application.get_response(
      '/facebook/browser/likes?id=tag:facebook.com,2013:123&token=towkin',
      method='POST', text=MBASIC_HTML_REACTIONS)

    self.assertEqual(200, resp.status_int, resp.text)
    self.assert_equals(MBASIC_REACTION_TAGS('123'), resp.json)

    activity = json_loads(key.get().activity_json)
    self.assert_equals(MBASIC_REACTION_TAGS('123'), activity['object']['tags'])

  def test_likes_bad_id(self):
    resp = app.application.get_response(
      '/facebook/browser/likes?id=789&token=towkin', method='POST', text='')
    self.assertEqual(400, resp.status_int)
    self.assertIn('Expected id to be tag URI', resp.text)

  def test_likes_no_activity(self):
    resp = app.application.get_response(
      '/facebook/browser/likes?id=tag:facebook.com,2013:123&token=towkin',
      method='POST', text='')
    self.assertEqual(404, resp.status_int)
    self.assertIn('No Facebook post found for id tag:facebook.com,2013:123',
                  resp.text)

  def test_likes_bad_token(self):
    self.store_activity()
    resp = app.application.get_response(
      '/facebook/browser/likes?id=tag:facebook.com,2013:123&token=nope',
      method='POST', text='')
    self.assertEqual(403, resp.status_int)
    self.assertIn("nope is not authorized for any of: {'snarfed.org'}", resp.text)

  def test_poll(self):
    key = self.fb.put()
    self.expect_task('poll', eta_seconds=0, source_key=key,
                     last_polled='1970-01-01-00-00-00')
    self.mox.ReplayAll()

    resp = app.application.get_response(
      f'/facebook/browser/poll?key={key.urlsafe().decode()}', method='POST')
    self.assertEqual(200, resp.status_int, resp.text)
    self.assertEqual('OK', resp.json)

  def test_poll_no_source(self):
    self.stub_create_task()
    resp = app.application.get_response(
      '/facebook/browser/poll?username=snarfed', method='POST')
    self.assertEqual(404, resp.status_int)
    self.assertIn('No account found for Facebook user snarfed', resp.text)
