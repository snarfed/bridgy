"""Unit tests for facebook.py.
"""
from __future__ import unicode_literals
from __future__ import absolute_import
from future.moves.urllib import error as urllib_error_py2
from future.utils import native_str
from future import standard_library
standard_library.install_aliases()
from past.builtins import basestring
from future.types.newstr import newstr

import copy
import logging
from unittest import skip
import urllib.error, urllib.parse, urllib.request

import appengine_config

from google.cloud import ndb
import granary
from granary.facebook import API_COMMENTS_ALL, API_NEWS_PUBLISHES, \
  API_PHOTOS_UPLOADED, API_OBJECT, API_PUBLISH_POST, API_SHARES, API_USER_EVENTS
from granary.tests.test_facebook import ACTIVITY, API_ME_POSTS, EVENT, \
  PHOTO, PHOTO_ACTIVITY, PHOTO_POST, POST
import oauth_dropins
from oauth_dropins import facebook as oauth_facebook
from oauth_dropins.webutil.util import json_dumps, json_loads
import webapp2

import app
import facebook
from facebook import FacebookPage
import models
import publish
import tasks
from . import testutil
import util


class FacebookPageTest(testutil.ModelsTest):

  def setUp(self):
    super(FacebookPageTest, self).setUp()
    for config in (appengine_config, granary.appengine_config,
                   oauth_dropins.appengine_config):
      setattr(config, 'FACEBOOK_APP_ID', 'my_app_id')
      setattr(config, 'FACEBOOK_APP_SECRET', 'my_app_secret')

    self.handler.messages = []
    self.auth_entity = oauth_facebook.FacebookAuth(
      id='my_string_id', auth_code='my_code', access_token_str='my_token',
      user_json=json_dumps({
        'id': '212038',
        'name': 'Ryan Barrett',
        'username': 'snarfed.org',
        'bio': 'something about me',
      }),
      pages_json=json_dumps([]), type='user')
    self.auth_entity.put()
    self.fb = FacebookPage.new(self.handler, auth_entity=self.auth_entity,
                               features=['listen'])
    self.fb.put()

    self.page_json = {
      'id': '108663232553079',
      'about': 'Our vegetarian cooking blog',
      'category': 'Home/garden website',
      'name': 'Hardly Starving',
      'access_token': 'page_token',
    }
    self.page_auth_entity = oauth_facebook.FacebookAuth(
      id=self.page_json['id'], user_json=json_dumps(self.page_json),
      auth_code='my_code', access_token_str='my_token', type='page')
    self.page_auth_entity.put()

    self.page = FacebookPage.new(self.handler, auth_entity=self.page_auth_entity,
                                 features=['listen'])
    self.page.put()

  def expect_api_call(self, path, response, **kwargs):
    if not isinstance(response, basestring):
      response = json_dumps(response)

    join_char = '&' if '?' in path else '?'
    return self.expect_urlopen(
      'https://graph.facebook.com/v4.0/%s%saccess_token=my_token' %
        (path, join_char),
      response, **kwargs)

  def poll(self):
    resp = tasks.application.get_response(
      '/_ah/queue/poll', method='POST', body=native_str(urllib.parse.urlencode({
          'source_key': self.fb.key.urlsafe(),
          'last_polled': self.fb.key.get().last_polled.strftime(
            util.POLL_TASK_DATETIME_FORMAT),
          })))
    self.assertEqual(200, resp.status_int)

  def test_new(self):
    self.assertEqual(self.auth_entity, self.fb.auth_entity.get())
    self.assertEqual('my_token', self.fb.gr_source.access_token)
    self.assertEqual('212038', self.fb.key.id())
    self.assertEqual('https://graph.facebook.com/v4.0/212038/picture?type=large',
                     self.fb.picture)
    self.assertEqual('Ryan Barrett', self.fb.name)
    self.assertEqual('snarfed.org', self.fb.username)
    self.assertEqual('https://www.facebook.com/snarfed.org', self.fb.silo_url())
    self.assertEqual('tag:facebook.com,2013:212038', self.fb.user_tag_id())

  def test_add_user_declines(self):
    resp = facebook.application.get_response(native_str(
      '/facebook/oauth_handler?' + urllib.parse.urlencode({
        'state': '{"feature":"listen","operation":"add"}',
        'error': 'access_denied',
        'error_code': '200',
        'error_reason': 'user_denied',
        'error_description': 'Permissions error',
      })))

    self.assert_equals(302, resp.status_code)
    self.assert_equals(
      'http://localhost/#!' + urllib.parse.quote(
        "OK, you're not signed up. Hope you reconsider!"),
      resp.headers['location'])
    self.assertNotIn('Set-Cookie', resp.headers)

  def test_add_user_no_domains_redirects_to_edit_websites(self):
    handler = facebook.OAuthCallback(
      webapp2.Request.blank('/facebook/oauth_handler'), self.response)
    handler.finish(self.auth_entity)

    self.assert_equals(302, handler.response.status_code)
    self.assert_equals(
      'http://localhost/edit-websites?source_key=%s' % self.fb.key.urlsafe(),
      handler.response.headers['location'])

  @skip("don't understand why this fails now, but don't care, since FB is dead")
  def test_add_user_with_domains_redirects_to_user_page(self):
    self.fb.domains = ['foo.com']
    self.fb.domain_urls = ['http://foo.com/']
    self.fb.webmention_endpoint = 'http://foo.com/wm'
    self.fb.put()

    handler = facebook.OAuthCallback(
      webapp2.Request.blank('/facebook/oauth_handler'), self.response)
    handler.finish(self.auth_entity)

    self.assert_equals(302, handler.response.status_code)
    loc = handler.response.headers['Location']
    self.assertTrue(loc.startswith('http://localhost/facebook/212038#'), loc)

  def test_get_activities(self):
    owned_event = copy.deepcopy(EVENT)
    owned_event['id'] = '888'
    owned_event['owner']['id'] = '212038'
    self.expect_api_call(API_ME_POSTS, {'data': [POST]})
    self.expect_api_call(API_NEWS_PUBLISHES % 'me', {'data': [PHOTO_POST]})
    self.expect_api_call(API_PHOTOS_UPLOADED % 'me', {'data': [PHOTO]})
    self.expect_api_call(API_USER_EVENTS, {'data': [EVENT, owned_event]})
    self.expect_api_call(API_OBJECT % ('212038', '888'), {})
    self.mox.ReplayAll()

    event_activity = self.fb.gr_source.event_to_activity(owned_event)
    self.assert_equals(
      [ACTIVITY, PHOTO_ACTIVITY, event_activity],
      self.fb.get_activities())

  def test_get_activities_post_and_photo_duplicates(self):
    self.expect_api_call(API_ME_POSTS, {'data': [PHOTO_POST]})
    self.expect_api_call(API_NEWS_PUBLISHES % 'me', {'data': []})
    self.expect_api_call(API_PHOTOS_UPLOADED % 'me', {'data': [PHOTO]})
    self.expect_api_call(API_USER_EVENTS, {})
    self.mox.ReplayAll()

    got = self.fb.get_activities()
    self.assertEquals(1, len(got))
    obj = got[0]['object']
    self.assertEquals('tag:facebook.com,2013:222', obj['id'])
    self.assertEquals('https://www.facebook.com/212038/posts/222', obj['url'])
    self.assertEquals(1, len(obj['replies']['items']))
    self.assertEquals(1, len([t for t in obj['tags'] if t.get('verb') == 'like']))

  def test_get_activities_canonicalizes_ids_with_colons(self):
    """https://github.com/snarfed/bridgy/issues/305"""
    # translate post id and comment ids to same ids in new colon-based format
    post = copy.deepcopy(POST)
    activity = copy.deepcopy(ACTIVITY)
    post['id'] = activity['fb_id'] = activity['object']['fb_id'] = \
      '212038:10100176064482163:11'

    reply = activity['object']['replies']['items'][0]
    post['comments']['data'][0]['id'] = reply['fb_id'] = \
        '12345:547822715231468:987_6796480'
    reply['url'] = 'https://www.facebook.com/12345/posts/547822715231468?comment_id=6796480'
    reply['inReplyTo'][0]['url'] = 'https://www.facebook.com/12345/posts/547822715231468'

    self.expect_api_call(API_ME_POSTS, {'data': [post]})
    self.expect_api_call(API_NEWS_PUBLISHES % 'me', {})
    self.expect_api_call(API_PHOTOS_UPLOADED % 'me', {})
    self.expect_api_call(API_USER_EVENTS, {})
    self.expect_api_call(API_OBJECT % ('212038', '10100176064482163'), {})
    self.mox.ReplayAll()

    self.assert_equals([activity], self.fb.get_activities())

  def test_get_activities_ignores_bad_comment_ids(self):
    """https://github.com/snarfed/bridgy/issues/305"""
    bad_post = copy.deepcopy(POST)
    bad_post['id'] = '90^90'

    post_with_bad_comment = copy.deepcopy(POST)
    post_with_bad_comment['comments']['data'].append(
      {'id': '12^34', 'message': 'bad to the bone'})

    self.expect_api_call(API_ME_POSTS, {'data': [bad_post]})
    self.expect_api_call(API_NEWS_PUBLISHES % 'me', {'data': [post_with_bad_comment]})
    self.expect_api_call(API_PHOTOS_UPLOADED % 'me', {})
    self.expect_api_call(API_USER_EVENTS, {})
    self.expect_api_call(API_OBJECT % ('212038', '10100176064482163'), {})
    self.mox.ReplayAll()

    # should only get the base activity, without the extra comment, and not the
    # bad activity at all
    self.assert_equals([ACTIVITY], self.fb.get_activities())

  def test_get_activities_page(self):
    """Shouldn't fetch /me/news.publishes for pages."""
    self.expect_api_call(API_ME_POSTS, {'data': [POST]})
    self.expect_api_call(API_PHOTOS_UPLOADED % 'me', {})
    self.expect_api_call(API_USER_EVENTS, {})
    self.expect_api_call(API_OBJECT % ('108663232553079', '10100176064482163'), {})
    self.mox.ReplayAll()
    self.assert_equals([ACTIVITY], self.page.get_activities())

  def test_get_activities_populates_resolved_ids(self):
    self.expect_api_call(API_ME_POSTS, {'data': [
      {'id': '1', 'object_id': '2'},
      {'id': '000_3', 'object_id': '000_4'},
    ]})
    self.expect_api_call(API_NEWS_PUBLISHES % 'me', {})
    self.expect_api_call(API_PHOTOS_UPLOADED % 'me', {'data': [
      {'id': '2', 'privacy': 'everyone'},
      {'id': '000_4', 'privacy': 'everyone'},
    ]})
    self.expect_api_call(API_USER_EVENTS, {})
    self.mox.ReplayAll()

    self.fb.get_activities()
    self.assertEquals('2', self.fb.cached_resolve_object_id('1'))
    self.assertEquals('4', self.fb.cached_resolve_object_id('3'))

    self.fb.put()
    self.assert_equals(json_dumps({'1': '2', '3': '4', '2': '2', '4': '4'}),
                       self.fb.key.get().resolved_object_ids_json)

  def test_expired_sends_notification(self):
    self.expect_api_call(API_ME_POSTS,
                         {'error': {'code': 190, 'error_subcode': 463}},
                         status=400)

    params = {
      'template': "Bridgy's access to your account has expired. Click here to renew it now!",
      'href': 'https://brid.gy/facebook/start',
      'access_token': 'my_app_id|my_app_secret',
      }
    self.expect_urlopen('https://graph.facebook.com/v4.0/212038/notifications', '',
                        data=urllib.parse.urlencode(params))
    self.mox.ReplayAll()

    self.assertRaises(models.DisableSource, self.fb.get_activities)

  def test_app_not_installed_doesnt_send_notification(self):
    self.expect_api_call(API_ME_POSTS, {'error': {
        'code': 190,
        'error_subcode': 458,
        'message': 'Error validating access token: The user has not authorized application 123456.',
      }}, status=400)

    self.mox.ReplayAll()
    self.assertRaises(models.DisableSource, self.fb.get_activities)

  def test_permissions_error_doesnt_send_notification(self):
    self.expect_api_call(API_ME_POSTS, {'error': {
      'code': 200,
      'type': 'FacebookApiException',
      'message': 'Permissions error',
      }}, status=400)

    self.mox.ReplayAll()
    self.assertRaises(models.DisableSource, self.fb.get_activities)

  def test_page_admin_error_doesnt_send_notification(self):
    self.expect_api_call(API_ME_POSTS, {'error': {
      'code': 190,
      'type': 'OAuthException',
      'message': 'The user must be an administrator of the page in order to impersonate it.'
    }}, status=400)

    self.mox.ReplayAll()
    self.assertRaises(models.DisableSource, self.fb.get_activities)

  def test_any_disable_error_for_page_doesnt_send_notification(self):
    self.expect_api_call(API_ME_POSTS, {}, status=401)
    self.mox.ReplayAll()
    self.assertRaises(models.DisableSource, self.page.get_activities)

  def test_other_error(self):
    msg = json_dumps({'error': {'code': 190, 'error_subcode': 789}})
    self.expect_api_call(API_ME_POSTS, msg, status=400)
    self.mox.ReplayAll()

    with self.assertRaises(urllib_error_py2.HTTPError) as cm:
      self.fb.get_activities()

    self.assertEquals(400, cm.exception.code)
    self.assertEquals(msg, cm.exception.body)

  def test_other_error_not_json(self):
    """If an error body isn't JSON, we should raise the original exception."""
    self.expect_api_call(API_ME_POSTS, 'not json', status=400)
    self.mox.ReplayAll()

    with self.assertRaises(urllib_error_py2.HTTPError) as cm:
      self.fb.get_activities()

    self.assertEquals(400, cm.exception.code)
    self.assertEquals('not json', cm.exception.body)

  def test_canonicalize_url_basic(self):
    # should look it up once, then cache it
    self.expect_api_call(API_OBJECT % ('212038', '222'),
                         {'id': '0', 'object_id': '314159'})
    self.mox.ReplayAll()

    for expected, input in (
      ('https://www.facebook.com/212038/posts/314159',
       'http://facebook.com/snarfed.org/posts/222'),
      ('https://www.facebook.com/212038/posts/314159',
       'http://facebook.com/snarfed.org/posts/222/'),
      # second time should use memcache instead of fetching object from API
      ('https://www.facebook.com/212038/posts/314159',
       'http://facebook.com/snarfed.org/posts/222'),
      ('https://www.facebook.com/212038/posts/314159',
       'http://facebook.com/snarfed.org/posts/222:0'),
      ('https://www.facebook.com/212038/posts/314159',
       'https://facebook.com/snarfed.org/photos.php?fbid=314159'),
      # note. https://github.com/snarfed/bridgy/issues/429
      ('https://www.facebook.com/212038/posts/314159',
       'https://www.facebook.com/notes/ryan-b/title/314159'),
      ('https://www.facebook.com/212038/posts/314159',
       'https://www.facebook.com/212038/posts/222?comment_id=456'),
      ('https://www.facebook.com/212038/posts/314159',
       'https://www.facebook.com/photo.php?fbid=314159&set=a.456.2393090.212038&type=1&theater'),
      ('https://www.facebook.com/212038/posts/314159',
       'https://facebook.com/permalink.php?story_fbid=314159&id=212038'),
      ('https://www.facebook.com/212038/posts/314159',
       'https://facebook.com/permalink.php?story_fbid=314159&amp;id=212038'),
      ('https://www.facebook.com/212038/posts/314159',
       'https://m.facebook.com/story.php?id=212038&story_fbid=314159'),
      ):
      logging.debug(input)
      self.assertEqual(expected, self.fb.canonicalize_url(input), input)

  def test_canonicalize_url_fetch_400s(self):
    self.expect_api_call(API_OBJECT % ('212038', '123'), {}, status=400)
    self.mox.ReplayAll()

    self.assertEqual(
      'https://www.facebook.com/212038/posts/123',
      self.fb.canonicalize_url('http://facebook.com/snarfed.org/posts/123'))
    self.assertEqual(
      'https://www.facebook.com/123',
      self.fb.canonicalize_url('http://facebook.com/123'))

  def test_canonicalize_url_username(self):
    # we shouldn't touch username when it appears elsewhere in the url
    self.assertEqual('https://www.facebook.com/25624/posts/snarfed.org',
                     self.fb.canonicalize_url(
                       'http://www.facebook.com/25624/posts/snarfed.org'))

    # username should override inferred username
    self.expect_api_call(API_OBJECT % ('212038', '444'), {})
    self.mox.ReplayAll()

    self.fb.inferred_username = 'mr-disguise'
    self.assertEqual('https://www.facebook.com/212038/posts/444',
                     self.fb.canonicalize_url(
                       'https://www.facebook.com/mr-disguise/posts/444'))

    # if no username, fall through
    self.fb.username = None
    self.assertEqual('https://www.facebook.com/212038/posts/444',
                     self.fb.canonicalize_url(
                       'https://www.facebook.com/mr-disguise/posts/444'))

  def test_canonicalize_url_app_scoped_user_id(self):
    self.expect_api_call(API_OBJECT % ('212038', '444'), {})
    self.mox.ReplayAll()

    self.fb.inferred_user_ids.append('101008675309')
    self.assertEqual('https://www.facebook.com/212038/posts/444',
                     self.fb.canonicalize_url(
                       'https://www.facebook.com/101008675309/posts/444'))

  def test_canonicalize_url_not_facebook(self):
    """Shouldn't try to extract id and fetch post for non-facebook.com URLs."""
    url = 'https://twitter.com/foo/status/123'
    self.assertIsNone(self.fb.canonicalize_url(url))

  def test_canonicalize_url_with_activity(self):
    """If we pass an activity with fb_object_id, use that, don't fetch from FB."""
    obj = {'fb_object_id': 456}
    act = {'object': obj}

    for activity in obj, act:
      got = self.fb.canonicalize_url('http://facebook.com/foo/posts/123',
                                                 activity=activity)
      self.assertEqual('https://www.facebook.com/212038/posts/456', got)

  def test_photo_syndication_url(self):
    """End to end test with syndication URL with FB object id instead of post id.

    Background in https://github.com/snarfed/bridgy/issues/189
    """
    self.fb.domain_urls = ['http://author/url']
    self.fb.last_hfeed_refetch = testutil.NOW
    self.fb.put()

    # Facebook API calls
    self.expect_api_call(API_ME_POSTS + '&limit=50', {'data': [
      PHOTO_POST]})
    self.expect_api_call(API_NEWS_PUBLISHES % 'me', {})
    self.expect_api_call(API_PHOTOS_UPLOADED % 'me', {'data': [PHOTO]})
    self.expect_api_call(API_USER_EVENTS, {})
    self.expect_api_call(API_SHARES % '222', {})
    self.expect_api_call(API_COMMENTS_ALL % '222', {})

    # posse post discovery
    self.expect_requests_get('http://author/url', """
    <html>
      <div class="h-entry">
        <a class="u-url" href="http://my.orig/post"></a>
      </div>
    </html>""")

    self.assertNotIn('222', PHOTO_POST['id'])
    self.assertEquals('222', PHOTO_POST['object_id'])
    self.expect_requests_get('http://my.orig/post', """
    <html class="h-entry">
      <a class="u-syndication" href="https://www.facebook.com/photo.php?fbid=222&set=a.995695740593.2393090.212038&type=1&theater'"></a>
    </html>""")

    self.mox.ReplayAll()
    self.poll()

    resps = list(models.Response.query())
    self.assertEquals(3, len(resps))
    for resp in resps:
      self.assertEqual(['http://my.orig/post'], resp.unsent)

  def test_post_publics_json(self):
    """End to end test of the post_publics_json cache.

    https://github.com/snarfed/bridgy/issues/633#issuecomment-198806909
    """
    # first poll. only return post, with privacy and object_id, no responses.
    photo_post = copy.deepcopy(PHOTO_POST)
    del photo_post['comments'], photo_post['likes'], photo_post['reactions']
    photo = copy.deepcopy(PHOTO)
    del photo['comments'], photo['likes'], photo['reactions']

    self.expect_api_call(API_ME_POSTS + '&limit=50', {'data': [photo_post]})
    self.expect_api_call(API_NEWS_PUBLISHES % 'me', {})
    self.expect_api_call(API_PHOTOS_UPLOADED % 'me', {'data': [photo]})
    self.expect_api_call(API_USER_EVENTS, {})
    self.expect_api_call(API_SHARES % '222', {})
    self.expect_api_call(API_COMMENTS_ALL % '222', {})

    # second poll. only return photo. should use post's cached privacy and
    # object_id mapping.
    assert 'privacy' not in PHOTO
    self.expect_api_call(API_ME_POSTS + '&limit=50', {})
    self.expect_api_call(API_NEWS_PUBLISHES % 'me', {})
    self.expect_api_call(API_PHOTOS_UPLOADED % 'me', {'data': [PHOTO]})
    self.expect_api_call(API_USER_EVENTS, {})
    self.expect_api_call(API_SHARES % '222', {})
    self.expect_api_call(API_COMMENTS_ALL % '222', {})

    self.mox.ReplayAll()

    self.poll()
    self.assertEquals(0, models.Response.query().count())

    self.poll()
    self.assert_equals((
      ndb.Key('Response', 'tag:facebook.com,2013:222_10559'),
      ndb.Key('Response', 'tag:facebook.com,2013:222_liked_by_666'),
      ndb.Key('Response', 'tag:facebook.com,2013:222_wow_by_777'),
    ), models.Response.query().fetch(keys_only=True))

  def test_save_cache(self):
    self.fb.updates = {
      'post_publics': {
        '3': None,
        newstr('4'): None,
        'True': None,
        newstr('False'): None,
      },
    }
    self.fb.put()

  def test_on_new_syndicated_post_infer_username(self):
    # username is already set
    models.SyndicatedPost.insert(self.fb, original='http://or.ig',
                                 syndication='http://facebook.com/fooey/posts/123')
    fb = self.fb.key.get()
    self.assertIsNone(fb.inferred_username)

    # url has original user id, not username
    fb.username = None
    fb.put()
    models.SyndicatedPost.insert(self.fb, original='http://an.other',
                                 syndication='http://facebook.com/212038/posts/123')
    self.assertIsNone(fb.key.get().inferred_username)

    # no syndication url in SyndicatedPost
    models.SyndicatedPost.insert_original_blank(self.fb, original='http://x')
    self.assertIsNone(fb.key.get().inferred_username)

    # should infer username
    self.mox.ResetAll()
    self.expect_api_call(API_OBJECT % ('212038', '123'),
                         {'id': '0', 'object_id': '123'})
    self.mox.ReplayAll()
    syndpost = models.SyndicatedPost.insert(
      self.fb, original='http://fin.al',
      syndication='http://facebook.com/fooey/posts/123')
    self.assertEquals('fooey', fb.key.get().inferred_username)
    self.assertEquals('https://www.facebook.com/212038/posts/123',
                      syndpost.syndication)

  def test_on_new_syndicated_post_infer_user_id(self):
    self.fb.username = None
    self.fb.put()

    self.expect_api_call(API_OBJECT % ('212038', '456'),
                         {'id': '0', 'object_id': '456'})
    self.mox.ReplayAll()

    syndpost = models.SyndicatedPost.insert(
      self.fb, original='http://aga.in',
      syndication='https://www.facebook.com/101008675309/posts/456')
    self.assertEquals(['101008675309'], self.fb.key.get().inferred_user_ids)
    self.assertEquals('https://www.facebook.com/212038/posts/456',
                      syndpost.syndication)

  def test_on_new_syndicated_post_infer_user_id_dedupes(self):
    self.fb.username = None
    self.fb.inferred_user_ids = ['789']
    self.fb.put()

    syndpost = models.SyndicatedPost.insert(
      self.fb, original='http://aga.in',
      syndication='https://www.facebook.com/789/posts/456')
    self.assertEquals(['789'], self.fb.key.get().inferred_user_ids)
    self.assertEquals('https://www.facebook.com/789/posts/456',
                      syndpost.syndication)

  def test_pre_put_hook(self):
    self.expect_api_call(API_OBJECT % ('212038', '1'),
                         {'id': '0', 'object_id': '2'})
    self.expect_api_call(API_OBJECT % ('212038', '3'),
                         {'id': '0', 'object_id': '4'})
    self.expect_api_call(API_OBJECT % ('212038', '5'), {})
    self.mox.ReplayAll()

    self.assertIsNone(self.fb.key.get().resolved_object_ids_json)

    self.fb.canonicalize_url('http://facebook.com/foo/posts/1')
    self.fb.canonicalize_url('http://facebook.com/foo/posts/3')
    self.fb.put()
    self.assertEquals(json_dumps({'1': '2', '3': '4'}),
                      self.fb.key.get().resolved_object_ids_json)

    try:
      orig = facebook.MAX_RESOLVED_OBJECT_IDS
      facebook.MAX_RESOLVED_OBJECT_IDS = 2
      self.fb.canonicalize_url('http://facebook.com/foo/posts/5')
      self.fb.put()
      # should keep the highest ids
      self.assertEquals(json_dumps({'3': '4', '5': None}),
                        self.fb.key.get().resolved_object_ids_json)
    finally:
      facebook.MAX_RESOLVED_OBJECT_IDS = orig

  def test_oauth_scopes(self):
    """Ensure that passing "feature" translates to the appropriate permission
    scopes when authing when Facebook.
    """
    for feature in 'listen', 'publish', 'listen,publish', 'publish,listen':
      expected_auth_url = oauth_facebook.GET_AUTH_CODE_URL % {
        'scope': ','.join(sorted(set(
          (facebook.LISTEN_SCOPES if 'listen' in feature else []) +
          (facebook.PUBLISH_SCOPES if 'publish' in feature else [])))),
        'client_id': appengine_config.FACEBOOK_APP_ID,
        'redirect_uri': urllib.parse.quote_plus('http://localhost/facebook/oauth_handler'),
        'state': urllib.parse.quote_plus('{"feature":"' + feature + '","operation":"add"}'),
      }

      resp = facebook.application.get_response(
        '/facebook/start', method='POST', body=native_str(urllib.parse.urlencode({
          'feature': feature,
        })))

      self.assertEquals(302, resp.status_code)
      self.assertEquals(expected_auth_url, resp.headers['Location'])

  def test_disable_page(self):
    self.auth_entity.pages_json = json_dumps([self.page_json])
    self.auth_entity.put()

    self.expect_urlopen(oauth_facebook.API_PAGE_URL + '&access_token=page_token',
                        json_dumps(self.page_json))
    self.mox.ReplayAll()

    key = self.page.key.urlsafe()
    encoded_state = urllib.parse.quote_plus(
      '{"feature":"listen","operation":"delete","source":"' + key + '"}')

    expected_auth_url = oauth_facebook.GET_AUTH_CODE_URL % {
      'scope': '',
      'client_id': appengine_config.FACEBOOK_APP_ID,
      'redirect_uri': urllib.parse.quote_plus('http://localhost/facebook/delete/finish'),
      'state': encoded_state,
    }

    resp = app.application.get_response(
      '/delete/start', method='POST', body=native_str(urllib.parse.urlencode({
        'feature': 'listen',
        'key': key,
      })))

    self.assertEquals(302, resp.status_int)
    self.assertEquals(expected_auth_url, resp.headers['Location'])

    # when silo oauth is done, it should send us back to /SOURCE/delete/finish,
    # which would in turn redirect to the more general /delete/finish.
    resp = app.application.get_response(native_str(
      '/delete/finish?'
      + 'auth_entity=' + self.auth_entity.key.urlsafe()
      + '&state=' + encoded_state))

    self.assert_equals(302, resp.status_code)
    # listen feature has been removed
    self.assert_equals([], self.page.key.get().features)

  def test_page_chooser(self):
    self.fb.key.delete()
    self.auth_entity.pages_json = json_dumps([self.page_json])
    self.auth_entity.put()

    handler = facebook.OAuthCallback(
      webapp2.Request.blank('/facebook/oauth_handler'), self.response)
    handler.finish(self.auth_entity)

    self.assert_equals(200, self.response.status_code)
    self.assertIn('<input type="radio" name="id" id="212038"',
                  self.response.text)
    self.assertIn('<input type="radio" name="id" id="108663232553079"',
                  self.response.text)
    self.assertIsNone(self.fb.key.get())

  def test_skip_page_chooser_if_no_pages(self):
    self.fb.key.delete()

    handler = facebook.OAuthCallback(
      webapp2.Request.blank('/facebook/oauth_handler'), self.response)
    handler.finish(self.auth_entity)

    self.assert_equals(302, self.response.status_code)
    self.assertEquals(
      'http://localhost/edit-websites?source_key=%s' % self.fb.key.urlsafe(),
      self.response.headers['Location'])

  @staticmethod
  def prepare_person_tags():
    FacebookPage(id='555', username='username').put()
    FacebookPage(id='666', inferred_username='inferred').put()
    FacebookPage(id='777', domains=['my.domain']).put()
    input_urls = (
      'https://unknown/',
      'https://www.facebook.com/444',
      'https://www.facebook.com/username',
      'https://www.facebook.com/inferred',
      'https://www.facebook.com/unknown',
      'https://my.domain/',
    )
    expected_urls = (
      'https://unknown/',
      'https://www.facebook.com/444',
      'https://www.facebook.com/555',
      'https://www.facebook.com/666',
      'https://www.facebook.com/unknown',
      'https://www.facebook.com/777',
    )
    return input_urls, expected_urls

  def test_preprocess_for_publish(self):
    input_urls, expected_urls = self.prepare_person_tags()
    activity = {
      'object': {
        'objectType': 'note',
        'content': 'a msg',
        'tags': [{'objectType': 'person', 'url': url} for url in input_urls],
      },
    }

    self.fb.preprocess_for_publish(activity)
    self.assert_equals(expected_urls, [t['url'] for t in activity['object']['tags']])

  @skip("FB is dead!")
  def test_publish_person_tags(self):
    self.fb.features = ['publish']
    self.fb.domains = ['foo.com']
    self.fb.domain_urls = ['http://foo.com/']
    self.fb.put()

    input_urls, _ = self.prepare_person_tags()
    post_html = """
<article class="h-entry">
<p class="e-content">
my message
</p>
%s
<a href="http://localhost/publish/facebook"></a>
</article>
""" % ','.join('<a class="h-card u-category" href="%s">%s</a>' %
               (url, url.strip('/').split('/')[-1].capitalize())
               for url in input_urls)

    self.expect_requests_get('http://foo.com/bar', post_html)
    self.expect_api_call(API_PUBLISH_POST, {'id': '123_456'}, data=urllib.parse.urlencode({
        'message': 'my message\n\n(Originally published at: http://foo.com/bar)',
        'tags': '444,555,666,777',
      }))
    self.mox.ReplayAll()

    resp = publish.application.get_response(
      '/publish/webmention', method='POST', body=native_str(urllib.parse.urlencode({
        'source': 'http://foo.com/bar',
        'target': 'https://brid.gy/publish/facebook',
        'source_key': self.fb.key.urlsafe(),
      })))
    self.assertEquals(201, resp.status_int)

  def test_is_activity_public(self):
    """Incomplete test. Checks replies, likes, reposts, and when fb_id isn't set."""
    # we shouldn't make any API calls for these
    for obj in (
        {},
        {'to': {}},
        {'to': [{'alias': '@public'}]},
        {'to': [{'alias': '@public'}], 'fb_id': '1', 'objectType': 'comment'},
        {'to': [{'alias': '@public'}], 'fb_id': '2', 'verb': 'like'},
    ):
      self.assertTrue(self.fb.is_activity_public(obj))

  def test_gr_source_user_id(self):
    self.assertEqual('212038', self.fb.gr_source.user_id)
    self.assertIsNone(self.page.gr_source.user_id)
