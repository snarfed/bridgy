"""Unit tests for facebook.py."""
import copy
from datetime import datetime
import logging

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

import browser
from facebook import Facebook
from models import Activity, Domain
from . import testutil


class FacebookTest(testutil.AppTest):

  def setUp(self):
    super().setUp()
    self.actor['numeric_id'] = '212038'
    self.source = Facebook.new(actor=self.actor)
    self.domain = Domain(id='snarfed.org', tokens=['towkin']).put()
    self.auth = f'token=towkin&key={self.source.key.urlsafe().decode()}'
    self.mox.StubOutWithMock(gr_facebook, 'now_fn')

  def get_response(self, path_query, auth=True, **kwargs):
    if auth and '?' not in path_query:
      path_query += f'?{self.auth}'
    return self.client.post(f'/facebook/browser/{path_query}', **kwargs)

  def store_activity(self):
    activity = copy.deepcopy(MBASIC_ACTIVITIES[0])
    activity['actor']['url'] = 'http://snarfed.org/'
    return Activity(id='tag:facebook.com,2013:123', source=self.source.key,
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
      self.assertEqual(expected, self.source.canonicalize_url(input), input)

  def test_canonicalize_url_username(self):
    # we shouldn't touch username when it appears elsewhere in the url
    self.source.username = 'snarfed'
    self.assertEqual('https://www.facebook.com/25624/posts/snarfed',
                     self.source.canonicalize_url(
                       'http://www.facebook.com/25624/posts/snarfed'))

    # if no username, fall through
    self.source.username = None
    self.assertEqual('https://www.facebook.com/212038/posts/444',
                     self.source.canonicalize_url(
                       'https://www.facebook.com/mr-disguise/posts/444'))

  def test_canonicalize_url_not_facebook(self):
    """Shouldn't try to extract id and fetch post for non-facebook.com URLs."""
    url = 'https://twitter.com/foo/status/123'
    self.assertIsNone(self.source.canonicalize_url(url))

  def test_profile_new_user(self):
    self.assertIsNone(Facebook.get_by_id('212038'))

    # webmention discovery
    self.expect_requests_get('https://snarfed.org/', '')
    self.mox.ReplayAll()

    resp = self.get_response('profile?token=towkin', data=MBASIC_HTML_ABOUT)
    self.assertEqual(200, resp.status_code, resp.get_data(as_text=True))
    self.assertEqual(self.source.key.urlsafe().decode(), resp.json)

    fb = Facebook.get_by_id('212038')
    self.assertEqual('Ryan Barrett', fb.name)
    self.assertEqual('https://scontent-sjc3-1.xx.fbcdn.net/v/t1.0-1/cp0/e15/q65/p74x74/39610935_10104076860151373_4179282966062563328_o.jpg?...', fb.picture)
    self.assertEqual(['https://snarfed.org/', 'https://foo.bar/'], fb.domain_urls)
    self.assertEqual(['snarfed.org', 'foo.bar'], fb.domains)

  def test_feed(self):
    self.source.put()
    gr_facebook.now_fn().MultipleTimes().AndReturn(datetime(1999, 1, 1))
    self.mox.ReplayAll()

    resp = self.get_response('feed', data=MBASIC_HTML_TIMELINE)
    self.assertEqual(200, resp.status_code, resp.get_data(as_text=True))
    self.assertEqual(MBASIC_ACTIVITIES, resp.json)

  def test_post(self):
    self.source.put()
    gr_facebook.now_fn().MultipleTimes().AndReturn(datetime(1999, 1, 1))
    self.mox.ReplayAll()

    resp = self.get_response('post', data=MBASIC_HTML_POST)
    self.assertEqual(200, resp.status_code, resp.get_data(as_text=True))
    self.assertEqual(MBASIC_ACTIVITY, resp.json)

    activities = Activity.query().fetch()
    self.assertEqual(1, len(activities))
    self.assertEqual(self.source.key, activities[0].source)
    self.assertEqual(MBASIC_ACTIVITY, json_loads(activities[0].activity_json))

  def test_post_empty(self):
    key = self.source.put()

    resp = self.get_response(f'post?token=towkin&key={key.urlsafe().decode()}',
                             data="""\
    <!DOCTYPE html>
    <html><body></body></html>""")
    self.assertEqual(400, resp.status_code, resp.get_data(as_text=True))
    self.assertEqual('Scrape error: no Facebook post found in HTML',
                     resp.get_data(as_text=True))

  def test_post_merge_comments(self):
    key = self.source.put()
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
    resp = self.get_response('post', data=MBASIC_HTML_POST)
    self.assertEqual(200, resp.status_code, resp.get_data(as_text=True))
    self.assert_equals(MBASIC_ACTIVITY, resp.json)

    activity = activity_key.get()
    self.assert_equals(MBASIC_ACTIVITY, json_loads(activity.activity_json))

  def test_likes(self):
    self.source.put()
    key = self.store_activity()
    resp = self.get_response(f'likes?id=tag:facebook.com,2013:123&{self.auth}',
                             data=MBASIC_HTML_REACTIONS)

    self.assertEqual(200, resp.status_code, resp.get_data(as_text=True))
    self.assert_equals(MBASIC_REACTION_TAGS('123'), resp.json)

    activity = json_loads(key.get().activity_json)
    self.assert_equals(MBASIC_REACTION_TAGS('123'), activity['object']['tags'])

  def test_poll(self):
    key = self.source.put()
    self.expect_task('poll-now', source_key=key, last_polled='1970-01-01-00-00-00')
    self.mox.ReplayAll()

    resp = self.get_response('poll')
    self.assertEqual(200, resp.status_code, resp.get_data(as_text=True))
    self.assertEqual('OK', resp.json)

  def test_silo_url(self):
    self.source.username = None
    self.assertEqual('https://www.facebook.com/212038', self.source.silo_url())

    self.source.username = 'foo'
    self.assertEqual('https://www.facebook.com/foo', self.source.silo_url())

    self.actor.update({
      'numeric_id': '1000000000000001',
      'username': None,
    })
    self.assertIsNone(Facebook.new(actor=self.actor).silo_url())
