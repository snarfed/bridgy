"""Unit tests for instagram.py.
"""
import copy
from unittest import skip
import urllib.request, urllib.parse, urllib.error

import appengine_config  # injects 2013 into tag URIs in test_instagram objects

from granary import instagram as gr_instagram
from granary.tests import test_instagram as gr_test_instagram
from oauth_dropins.webutil.testutil import TestCase
from oauth_dropins.webutil.util import json_dumps, json_loads
import requests

import app
from instagram import Instagram
from models import Activity
from .testutil import ModelsTest, instagram_profile_user
import util


PROFILE_USER = copy.deepcopy(
  gr_test_instagram.HTML_PROFILE['entry_data']['ProfilePage'][0]['graphql']['user'])
PROFILE_USER['id'] = '987'


class InstagramTest(ModelsTest):

  def setUp(self):
    super(InstagramTest, self).setUp()
    self.handler.messages = []
    self.inst = Instagram.new(
      self.handler, actor={
        'objectType': 'person',
        'id': 'tag:instagram.com,2013:420973239',
        'username': 'snarfed',
        'displayName': 'Ryan Barrett',
        'url': 'https://snarfed.org/',
        'image': {'url': 'http://pic.ture/url'},
        # ...
      })

  def expect_webmention_discovery(self):
    return self.expect_requests_get('https://snarfed.org/', '', stream=None,
                                    verify=False)

  def test_new(self):
    self.assertIsNone(self.inst.auth_entity)
    self.assertEqual('snarfed', self.inst.key.string_id())
    self.assertEqual('http://pic.ture/url', self.inst.picture)
    self.assertEqual('https://www.instagram.com/snarfed/', self.inst.url)
    self.assertEqual('https://www.instagram.com/snarfed/', self.inst.silo_url())
    # self.assertEqual('tag:instagram.com,2013:420973239', self.inst.user_tag_id())
    self.assertEqual('Ryan Barrett', self.inst.name)
    self.assertEqual('snarfed (Instagram)', self.inst.label())

  def test_canonicalize_url(self):
    self.unstub_requests_head()
    for url in (
        'http://www.instagram.com/p/abcd',
        'https://www.instagram.com/p/abcd',
        'https://www.instagram.com/p/abcd/',
        'https://instagram.com/p/abcd',
    ):
      self.assertEqual('https://www.instagram.com/p/abcd/',
                       self.inst.canonicalize_url(url))

    self.assertIsNone(self.inst.canonicalize_url('https://www.foo.com/p/abcd/'))

  def test_canonicalize_url_approve_checks_full_url(self):
    """...specifically, that the regex ends with a $
    https://github.com/snarfed/bridgy/issues/686
    """
    self.assertEqual('https://www.instagram.com/p/abcd/123/',
                     self.inst.canonicalize_url('https://www.instagram.com/p/abcd/123'))

  def test_homepage(self):
    resp = app.application.get_response(
      '/instagram/browser/homepage', method='POST',
      text=gr_test_instagram.HTML_FEED_COMPLETE)
    self.assertEqual(200, resp.status_int)
    self.assertEqual('snarfed', resp.text)

  def test_homepage_bad_html(self):
    resp = app.application.get_response(
      '/instagram/browser/homepage', method='POST',
      text='not a logged in IG feed')
    self.assertEqual(400, resp.status_int)
    self.assertIn("Couldn't determine logged in Instagram user", resp.text)

  def test_profile_new_user(self):
    self.assertIsNone(Instagram.get_by_id('snarfed'))

    self.expect_webmention_discovery()
    self.mox.ReplayAll()

    resp = app.application.get_response(
      '/instagram/browser/profile', method='POST',
      text=gr_test_instagram.HTML_PROFILE_COMPLETE)

    self.assertEqual(200, resp.status_int)
    self.assertEqual([
      gr_test_instagram.HTML_PHOTO_ACTIVITY,
      gr_test_instagram.HTML_VIDEO_ACTIVITY,
    ], resp.json)

    ig = Instagram.get_by_id('snarfed')
    self.assertEqual('Ryan B', ig.name)
    self.assertEqual('https://scontent-sjc2-1.cdninstagram.com/hphotos-xfa1/t51.2885-19/11373714_959073410822287_2004790583_a.jpg', ig.picture)
    self.assertEqual('https://www.instagram.com/snarfed/', ig.url)
    self.assertEqual(['https://snarfed.org/'], ig.domain_urls)
    self.assertEqual(['snarfed.org'], ig.domains)

  def test_profile_private_account(self):
    resp = app.application.get_response(
      '/instagram/browser/profile', method='POST',
      text=gr_test_instagram.HTML_PROFILE_PRIVATE_COMPLETE)
    self.assertEqual(400, resp.status_int)
    self.assertIn('Your Instagram account is private.', resp.text)

  def test_post(self):
    source = Instagram.create_new(self.handler, actor={'username': 'jc'})

    resp = app.application.get_response(
      '/instagram/browser/post', method='POST',
      text=gr_test_instagram.HTML_VIDEO_COMPLETE)
    self.assertEqual(200, resp.status_int, resp.text)
    self.assertEqual(gr_test_instagram.HTML_VIDEO_ACTIVITY_FULL, resp.json)

    activities = Activity.query().fetch()
    self.assertEqual(1, len(activities))
    self.assertEqual(source.key, activities[0].source)
    self.assertEqual(gr_test_instagram.HTML_VIDEO_ACTIVITY_FULL,
                     json_loads(activities[0].activity_json))

  def test_post_no_source(self):
    resp = app.application.get_response(
      '/instagram/browser/post', method='POST',
      text=gr_test_instagram.HTML_VIDEO_COMPLETE)
    self.assertEqual(404, resp.status_int)
    self.assertIn('No account found for Instagram user jc', resp.text)

  def test_post_empty(self):
    resp = app.application.get_response(
      '/instagram/browser/post', method='POST', text='')
    self.assertEqual(400, resp.status_int)
    self.assertIn('Expected 1 Instagram post', resp.text)

  def test_post_merge_comments(self):
    # TODO
    pass

  def test_likes(self):
    activity_json = json_dumps(gr_test_instagram.HTML_PHOTO_ACTIVITY)
    a = Activity(id='tag:instagram.com,2013:123_456', activity_json=activity_json).put()

    resp = app.application.get_response(
      '/instagram/browser/likes?id=tag:instagram.com,2013:123_456', method='POST',
      text=json_dumps(gr_test_instagram.LIKES))
    self.assertEqual(200, resp.status_int, resp.text)

    expected_likes = copy.deepcopy(gr_test_instagram.LIKE_OBJS)
    for like in expected_likes:
      del like['object']
      del like['url']
    self.assertEqual(expected_likes, resp.json)

    activity = json_loads(a.get().activity_json)
    self.assertEqual(expected_likes, activity['object']['tags'])

  def test_likes_bad_id(self):
    resp = app.application.get_response(
      '/instagram/browser/likes?id=789', method='POST',
      text=json_dumps(gr_test_instagram.LIKES))
    self.assertEqual(400, resp.status_int)
    self.assertIn('Expected id to be tag URI', resp.text)

  def test_likes_no_activity(self):
    resp = app.application.get_response(
      '/instagram/browser/likes?id=tag:instagram.com,2013:789', method='POST',
      text=json_dumps(gr_test_instagram.LIKES))
    self.assertEqual(404, resp.status_int)
    self.assertIn('No Instagram post found for id tag:instagram.com,2013:789',
                  resp.text)

  def test_poll(self):
    source = Instagram.create_new(self.handler, actor={'username': 'snarfed'})
    self.expect_task('poll', eta_seconds=0, source_key=source.key,
                     last_polled='1970-01-01-00-00-00')
    self.mox.ReplayAll()
    resp = app.application.get_response(
      '/instagram/browser/poll?username=snarfed', method='POST')

  def test_poll_no_source(self):
    self.stub_create_task()
    resp = app.application.get_response(
      '/instagram/browser/poll?username=snarfed', method='POST')
    self.assertEqual(404, resp.status_int)
    self.assertIn('No account found for Instagram user snarfed', resp.text)
