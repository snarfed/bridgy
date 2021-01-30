"""Unit tests for browser.py.
"""
import copy

from oauth_dropins.webutil.testutil import TestCase
from oauth_dropins.webutil.util import json_dumps, json_loads
from oauth_dropins.webutil import util
import webapp2

import browser
from models import Activity, Domain
from .testutil import FakeGrSource, FakeSource, ModelsTest


class FakeBrowserSource(browser.BrowserSource):
  GR_CLASS = FakeGrSource
  SHORT_NAME = 'fbs'
  gr_source = FakeGrSource()


class BrowserSourceTest(ModelsTest):

  def setUp(self):
    super(BrowserSourceTest, self).setUp()
    self.handler.messages = []
    self.inst = FakeBrowserSource.new(self.handler, actor=self.actor)
    FakeBrowserSource.gr_source.actor = {}

  def test_new(self):
    self.assertIsNone(self.inst.auth_entity)
    self.assertEqual('snarfed', self.inst.key.string_id())
    self.assertEqual('http://fa.ke/snarfed', self.inst.url)
    self.assertEqual('Ryan B', self.inst.name)
    self.assertEqual('Ryan B (FakeSource)', self.inst.label())

  def test_get_activities_response_activity_id(self):
    Activity(id='tag:fa.ke,2013:123',
             activity_json=json_dumps({'foo': 'bar'})).put()

    resp = self.inst.get_activities_response(activity_id='123')
    self.assertEqual([{'foo': 'bar'}], resp['items'])

  def test_get_activities_response_no_activity_id(self):
    Activity(id='tag:fa.ke,2013:123', source=self.inst.key,
             activity_json=json_dumps({'foo': 'bar'})).put()
    Activity(id='tag:fa.ke,2013:456', source=self.inst.key,
             activity_json=json_dumps({'baz': 'biff'})).put()

    other = FakeBrowserSource.new(self.handler, actor={'username': 'other'}).put()
    Activity(id='tag:fa.ke,2013:789', source=other,
             activity_json=json_dumps({'boo': 'bah'})).put()


    resp = self.inst.get_activities_response()
    self.assert_equals([{'foo': 'bar'}, {'baz': 'biff'}], resp['items'])

  def test_get_activities_response_no_stored_activity(self):
    resp = self.inst.get_activities_response(activity_id='123')
    self.assertEqual([], resp['items'])

  def test_get_comment(self):
    self.assert_equals(
      self.activities[0]['object']['replies']['items'][0],
      self.inst.get_comment('1_2_a', activity=self.activities[0]))

  def test_get_comment_no_matching_id(self):
    self.assertIsNone(self.inst.get_comment('333', activity=self.activities[0]))

  def test_get_comment_no_activity_kwarg(self):
    self.assertIsNone(self.inst.get_comment('020'))

  def test_get_like(self):
    self.assert_equals(
      self.activities[0]['object']['tags'][0],
      self.inst.get_like('unused', 'unused', 'alice', activity=self.activities[0]))

  def test_get_like_no_matching_user(self):
    self.assertIsNone(self.inst.get_like(
      'unused', 'unused', 'eve', activity=self.activities[0]))

  def test_get_like_no_activity_kwarg(self):
    self.assertIsNone(self.inst.get_like('unused', 'unused', 'alice'))


class BrowserHandlerTest(ModelsTest):
  app = webapp2.WSGIApplication(browser.routes(FakeBrowserSource))

  def setUp(self):
    super().setUp()
    self.domain = Domain(id='snarfed.org', tokens=['towkin']).put()
    FakeBrowserSource.gr_source = FakeGrSource()
    self.source = FakeBrowserSource.new(self.handler, actor=self.actor).put()

    for a in self.activities:
      a['object']['author'] = self.actor

    self.activities_no_extras = copy.deepcopy(self.activities)
    for a in self.activities_no_extras:
      del a['object']['tags']

    self.activities_no_replies = copy.deepcopy(self.activities_no_extras)
    for a in self.activities_no_replies:
      del a['object']['replies']

  def store_activity(self):
    return Activity(id='tag:fa.ke,2013:123_456',
                    activity_json=json_dumps(self.activities[0])).put()

  def test_homepage(self):
    resp = self.app.get_response(
      '/fbs/browser/homepage', method='POST', text='homepage html')
    self.assertEqual(200, resp.status_int)
    self.assertEqual('snarfed', resp.json)

  def test_homepage_no_logged_in_user(self):
    FakeBrowserSource.gr_source.actor = {}
    resp = self.app.get_response(
      '/fbs/browser/homepage', method='POST', text='not logged in')
    self.assertEqual(400, resp.status_int)
    self.assertIn("Couldn't determine logged in FakeSource user", resp.text)

  def test_profile_new_user(self):
    self.source.delete()

    # webmention endpoint discovery
    self.expect_requests_get('https://snarfed.org/', '', stream=None, verify=False)
    self.mox.ReplayAll()

    resp = self.app.get_response('/fbs/browser/profile?token=towkin', method='POST')

    self.assertEqual(200, resp.status_int)
    self.assert_equals(self.activities_no_replies, util.trim_nulls(resp.json))

    src = FakeBrowserSource.get_by_id('snarfed')
    self.assertEqual('Ryan B', src.name)
    self.assertEqual('http://fa.ke/snarfed', src.url)
    self.assertEqual(['https://snarfed.org/'], src.domain_urls)
    self.assertEqual(['snarfed.org'], src.domains)

  def test_profile_fall_back_to_scraped_to_actor(self):
    self.source.delete()

    self.mox.StubOutWithMock(FakeGrSource, 'scraped_to_activities')
    FakeGrSource.scraped_to_activities('').AndReturn(([], None))

    # webmention endpoint discovery
    self.expect_requests_get('https://snarfed.org/', '', stream=None, verify=False)
    self.mox.ReplayAll()

    resp = self.app.get_response('/fbs/browser/profile?token=towkin', method='POST')
    self.assertEqual(200, resp.status_int)
    self.assert_equals([], resp.json)

    src = FakeBrowserSource.get_by_id('snarfed')
    self.assertEqual('Ryan B', src.name)
    self.assertEqual('http://fa.ke/snarfed', src.url)
    self.assertEqual(['https://snarfed.org/'], src.domain_urls)
    self.assertEqual(['snarfed.org'], src.domains)

  def test_profile_private_account(self):
    FakeBrowserSource.gr_source.actor['to'] = \
      [{'objectType':'group', 'alias':'@private'}]
    resp = self.app.get_response('/fbs/browser/profile?token=towkin', method='POST')
    self.assertEqual(400, resp.status_int)
    self.assertIn('Your FakeSource account is private.', resp.text)

  def test_profile_missing_token(self):
    resp = self.app.get_response('/fbs/browser/profile', method='POST')
    self.assertEqual(400, resp.status_int)
    self.assertIn('Missing required parameter: token', resp.text)

  def test_profile_no_stored_token(self):
    self.domain.delete()
    resp = self.app.get_response('/fbs/browser/profile?token=towkin', method='POST')
    self.assertEqual(400, resp.status_int)
    self.assertIn("towkin is not authorized for any of: {'snarfed.org'}", resp.text)

  def test_profile_bad_token(self):
    resp = self.app.get_response('/fbs/browser/profile?token=nope', method='POST')
    self.assertEqual(400, resp.status_int)
    self.assertIn("nope is not authorized for any of: {'snarfed.org'}", resp.text)

  def test_post(self):
    source = FakeBrowserSource.create_new(self.handler, actor={'username': 'snarfed'})

    resp = self.app.get_response('/fbs/browser/post?token=towkin', method='POST')
    self.assertEqual(200, resp.status_int, resp.text)
    self.assert_equals(self.activities_no_extras[0], util.trim_nulls(resp.json))

    activities = Activity.query().fetch()
    self.assertEqual(1, len(activities))
    self.assertEqual(source.key, activities[0].source)
    self.assert_equals(self.activities_no_extras[0],
                     util.trim_nulls(json_loads(activities[0].activity_json)))

  def test_post_no_source(self):
    FakeGrSource.activities[0]['object']['author']['username'] = 'unknown'
    resp = self.app.get_response('/fbs/browser/post?token=towkin', method='POST')
    self.assertEqual(404, resp.status_int)
    self.assertIn('No account found for FakeSource user unknown', resp.text)

  def test_post_empty(self):
    FakeGrSource.activities = []
    resp = self.app.get_response('/fbs/browser/post?token=towkin', method='POST')
    self.assertEqual(400, resp.status_int)
    self.assertIn('No FakeSource post found in HTML', resp.text)

  def test_post_missing_token(self):
    resp = self.app.get_response('/fbs/browser/post', method='POST')
    self.assertEqual(400, resp.status_int)
    self.assertIn('Missing required parameter: token', resp.text)

  def test_post_merge_comments(self):
    source = FakeBrowserSource.create_new(self.handler, actor={'username': 'snarfed'})

    # existing activity with two comments
    activity = copy.deepcopy(self.activities_no_extras[0])
    reply = self.activities[0]['object']['replies']['items'][0]
    activity['object']['replies'] = {
      'items': [reply, copy.deepcopy(reply)],
      'totalItems': 2,
    }
    activity['object']['replies']['items'][1]['id'] = 'abc'
    key = Activity(id=activity['id'], activity_json=json_dumps(activity)).put()

    # scraped activity has different second comment
    activity['object']['replies']['items'][1]['id'] = 'xyz'
    FakeGrSource.activities = [activity]

    resp = self.app.get_response('/fbs/browser/post?token=towkin', method='POST')
    self.assertEqual(200, resp.status_int, resp.text)
    self.assert_equals(activity, util.trim_nulls(resp.json))

    merged = json_loads(key.get().activity_json)
    replies = merged['object']['replies']
    self.assert_equals(3, replies['totalItems'], replies)
    self.assert_equals([reply['id'], 'abc', 'xyz'],
                       [r['id'] for r in replies['items']])

  def test_likes(self):
    key = self.store_activity()
    like = FakeBrowserSource.gr_source.like = {
      'objectType': 'activity',
      'verb': 'like',
      'id': 'new',
    }

    resp = self.app.get_response(
      '/fbs/browser/likes?id=tag:fa.ke,2013:123_456&token=towkin',
      method='POST')
    self.assertEqual(200, resp.status_int, resp.text)
    self.assert_equals([like], resp.json)

    stored = json_loads(key.get().activity_json)
    self.assert_equals(self.activities[0]['object']['tags'] + [like],
                       stored['object']['tags'])

  def test_likes_bad_id(self):
    resp = self.app.get_response(
      '/fbs/browser/likes?id=789&token=towkin', method='POST')
    self.assertEqual(400, resp.status_int)
    self.assertIn('Expected id to be tag URI', resp.text)

  def test_likes_no_activity(self):
    resp = self.app.get_response(
      '/fbs/browser/likes?id=tag:fa.ke,2013:789&token=towkin', method='POST')
    self.assertEqual(404, resp.status_int)
    self.assertIn('No FakeSource post found for id tag:fa.ke,2013:789', resp.text)

  def test_likes_bad_token(self):
    self.store_activity()
    resp = self.app.get_response(
      '/fbs/browser/likes?id=tag:fa.ke,2013:123_456&token=nope', method='POST')
    self.assertEqual(400, resp.status_int)
    self.assertIn("nope is not authorized for any of: {'snarfed.org'}", resp.text)

  def test_poll(self):
    source = FakeBrowserSource.create_new(self.handler, actor={'username': 'snarfed'})
    self.expect_task('poll', eta_seconds=0, source_key=source.key,
                     last_polled='1970-01-01-00-00-00')
    self.mox.ReplayAll()
    resp = self.app.get_response(
      '/fbs/browser/poll?username=snarfed', method='POST')
    self.assertEqual(200, resp.status_int, resp.text)
    self.assertEqual('OK', resp.json)

  def test_poll_no_source(self):
    self.source.delete()
    resp = self.app.get_response('/fbs/browser/poll?username=snarfed', method='POST')
    self.assertEqual(404, resp.status_int)
    self.assertIn('No account found for FakeSource user snarfed', resp.text)

  def test_token_domains(self):
    resp = self.app.get_response(
      '/fbs/browser/token-domains?token=towkin', method='POST')
    self.assertEqual(200, resp.status_int)
    self.assertEqual(['snarfed.org'], resp.json)

  def test_token_domains_missing(self):
    resp = self.app.get_response(
      '/fbs/browser/token-domains?token=unknown', method='POST')
    self.assertEqual(404, resp.status_int)
