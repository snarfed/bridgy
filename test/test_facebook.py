"""Unit tests for facebook.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import app
import copy
import datetime
import logging
import json
import re
import urllib
import urllib2

import appengine_config

import granary
from granary import facebook as gr_facebook
from granary.test import test_facebook as gr_test_facebook
import oauth_dropins
from oauth_dropins import facebook as oauth_facebook
import webapp2

import facebook
from facebook import FacebookPage
import models
import publish
import tasks
import testutil


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
      user_json=json.dumps({'id': '212038',
                            'name': 'Ryan Barrett',
                            'username': 'snarfed.org',
                            'bio': 'something about me',
                            'type': 'user',
                            }),
      pages_json=json.dumps([]))
    self.auth_entity.put()
    self.fb = FacebookPage.new(self.handler, auth_entity=self.auth_entity,
                               features=['listen'])
    self.fb.put()

    self.page = {
      'id': '108663232553079',
      'about': 'Our vegetarian cooking blog',
      'category': 'Home/garden website',
      'name': 'Hardly Starving',
      'type': 'page',
      'access_token': 'page_token',
    }

  def expect_api_call(self, path, response, **kwargs):
    if not isinstance(response, basestring):
      response = json.dumps(response)

    join_char = '&' if '?' in path else '?'
    return self.expect_urlopen(
      'https://graph.facebook.com/v2.2/%s%saccess_token=my_token' %
        (path, join_char),
      response, **kwargs)

  def test_new(self):
    self.assertEqual(self.auth_entity, self.fb.auth_entity.get())
    self.assertEqual('my_token', self.fb.gr_source.access_token)
    self.assertEqual('212038', self.fb.key.id())
    self.assertEqual('https://graph.facebook.com/v2.2/212038/picture?type=large',
                     self.fb.picture)
    self.assertEqual('Ryan Barrett', self.fb.name)
    self.assertEqual('snarfed.org', self.fb.username)
    self.assertEqual('user', self.fb.type)
    self.assertEqual('https://www.facebook.com/snarfed.org', self.fb.silo_url())
    self.assertEqual('tag:facebook.com,2013:212038', self.fb.user_tag_id())

  def test_add_user_declines(self):
    resp = facebook.application.get_response(
      '/facebook/oauth_handler?' + urllib.urlencode({
        'state': '{"feature":"listen","operation":"add"}',
        'error': 'access_denied',
        'error_code': '200',
        'error_reason': 'user_denied',
        'error_description': 'Permissions error',
      }))

    self.assert_equals(302, resp.status_code)
    self.assert_equals(
      'http://localhost/#!' + urllib.quote(
        "OK, you're not signed up. Hope you reconsider!"),
      resp.headers['location'])
    self.assertNotIn('Set-Cookie', resp.headers)

  def test_get_activities(self):
    owned_event = copy.deepcopy(gr_test_facebook.EVENT)
    owned_event['id'] = '888'
    owned_event['owner']['id'] = '212038'
    self.expect_api_call('me/feed?offset=0', {'data': [
      gr_test_facebook.POST, gr_test_facebook.PHOTO_POST]})
    self.expect_api_call('me/photos/uploaded', {'data': [gr_test_facebook.PHOTO]})
    self.expect_api_call('me/events', {'data': [gr_test_facebook.EVENT, owned_event]})
    self.expect_api_call(gr_facebook.API_EVENT % '145304994', gr_test_facebook.EVENT)
    self.expect_api_call(gr_facebook.API_EVENT % '888', owned_event)
    self.expect_api_call('888/invited', {'data': gr_test_facebook.RSVPS})
    self.mox.ReplayAll()

    event_activity = self.fb.gr_source.event_to_activity(owned_event)
    for k in 'attending', 'notAttending', 'maybeAttending', 'invited':
      event_activity['object'][k] = gr_test_facebook.EVENT_OBJ_WITH_ATTENDEES[k]
    self.assert_equals(
      [gr_test_facebook.ACTIVITY, gr_test_facebook.PHOTO_ACTIVITY, event_activity],
      self.fb.get_activities())

  def test_get_activities_post_and_photo_duplicates(self):
    self.expect_api_call('me/feed?offset=0', {'data': [gr_test_facebook.PHOTO_POST]})
    self.expect_api_call('me/photos/uploaded', {'data': [gr_test_facebook.PHOTO]})
    self.expect_api_call('me/events', {})
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
    post = copy.deepcopy(gr_test_facebook.POST)
    activity = copy.deepcopy(gr_test_facebook.ACTIVITY)
    post['id'] = activity['fb_id'] = activity['object']['fb_id'] = \
      '212038:10100176064482163:11'

    reply = activity['object']['replies']['items'][0]
    post['comments']['data'][0]['id'] = reply['fb_id'] = \
        '12345:547822715231468:987_6796480'
    reply['url'] = 'https://www.facebook.com/12345/posts/547822715231468?comment_id=6796480'
    reply['inReplyTo'][0]['url'] = 'https://www.facebook.com/12345/posts/547822715231468'

    self.expect_api_call('me/feed?offset=0', {'data': [post]})
    self.expect_api_call('me/photos/uploaded', {'data': []})
    self.expect_api_call('me/events', {})
    self.mox.ReplayAll()

    self.assert_equals([activity], self.fb.get_activities())

  def test_get_activities_ignores_bad_comment_ids(self):
    """https://github.com/snarfed/bridgy/issues/305"""
    bad_post = copy.deepcopy(gr_test_facebook.POST)
    bad_post['id'] = '90^90'

    post_with_bad_comment = copy.deepcopy(gr_test_facebook.POST)
    post_with_bad_comment['comments']['data'].append(
      {'id': '12^34', 'message': 'bad to the bone'})

    self.expect_api_call('me/feed?offset=0', {'data': [bad_post, post_with_bad_comment]})
    self.expect_api_call('me/photos/uploaded', {'data': []})
    self.expect_api_call('me/events', {})
    self.mox.ReplayAll()

    # should only get the base activity, without the extra comment, and not the
    # bad activity at all
    self.assert_equals([gr_test_facebook.ACTIVITY], self.fb.get_activities())

  def test_expired_sends_notification(self):
    self.expect_api_call('me/feed?offset=0',
                         {'error': {'code': 190, 'error_subcode': 463}},
                         status=400)

    params = {
      'template': "Brid.gy's access to your account has expired. Click here to renew it now!",
      'href': 'https://brid.gy/facebook/start',
      'access_token': 'my_app_id|my_app_secret',
      }
    self.expect_urlopen('https://graph.facebook.com/v2.2/212038/notifications', '',
                        data=urllib.urlencode(params))
    self.mox.ReplayAll()

    self.assertRaises(models.DisableSource, self.fb.get_activities)

  def test_app_not_installed_doesnt_send_notification(self):
    self.expect_api_call('me/feed?offset=0', {'error': {
        'code': 190,
        'error_subcode': 458,
        'message': 'Error validating access token: The user has not authorized application 123456.',
      }}, status=400)

    self.mox.ReplayAll()
    self.assertRaises(models.DisableSource, self.fb.get_activities)

  def test_permissions_error_doesnt_send_notification(self):
    self.expect_api_call('me/feed?offset=0', {'error': {
      'code': 200,
      'type': 'FacebookApiException',
      'message': 'Permissions error',
      }}, status=400)

    self.mox.ReplayAll()
    self.assertRaises(models.DisableSource, self.fb.get_activities)

  def test_page_admin_error_doesnt_send_notification(self):
    self.expect_api_call('me/feed?offset=0', {'error': {
      'code': 190,
      'type': 'OAuthException',
      'message': 'The user must be an administrator of the page in order to impersonate it.'
    }}, status=400)

    self.mox.ReplayAll()
    self.assertRaises(models.DisableSource, self.fb.get_activities)

  def test_other_error(self):
    msg = json.dumps({'error': {'code': 190, 'error_subcode': 789}})
    self.expect_api_call('me/feed?offset=0', msg, status=400)
    self.mox.ReplayAll()

    with self.assertRaises(urllib2.HTTPError) as cm:
      self.fb.get_activities()

    self.assertEquals(400, cm.exception.code)
    self.assertEquals(msg, cm.exception.body)

  def test_other_error_not_json(self):
    """If an error body isn't JSON, we should raise the original exception."""
    self.expect_api_call('me/feed?offset=0', 'not json', status=400)
    self.mox.ReplayAll()

    with self.assertRaises(urllib2.HTTPError) as cm:
      self.fb.get_activities()

    self.assertEquals(400, cm.exception.code)
    self.assertEquals('not json', cm.exception.body)

  def test_canonicalize_syndication_url_basic(self):
    # should look it up once, then cache it
    self.expect_api_call('212038_222', {'id': '0', 'object_id': '314159'})
    self.mox.ReplayAll()

    for expected, input in (
      ('https://www.facebook.com/212038/posts/314159',
       'http://facebook.com/snarfed.org/posts/222'),
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
       'https://www.facebook.com/photo.php?fbid=314159&set=a.456.2393090.212038&type=1&theater'),
      ('https://www.facebook.com/212038/posts/314159',
       'https://facebook.com/permalink.php?story_fbid=314159&id=212038'),
      ('https://www.facebook.com/212038/posts/314159',
       'https://facebook.com/permalink.php?story_fbid=314159&amp;id=212038'),
      ('https://www.facebook.com/212038/posts/314159',
       'https://m.facebook.com/story.php?id=212038&story_fbid=314159'),
      ):
      logging.debug(input)
      self.assertEqual(expected, self.fb.canonicalize_syndication_url(input))

  def test_canonicalize_syndication_url_fetch_400s(self):
    self.expect_api_call('212038_123', {}, status=400)
    self.mox.ReplayAll()

    self.assertEqual('https://www.facebook.com/212038/posts/123',
                     self.fb.canonicalize_syndication_url(
                       'http://facebook.com/snarfed.org/posts/123'))

  def test_canonicalize_syndication_url_username(self):
    for id in '212038_snarfed.org', '212038_444':
      self.expect_api_call(id, {})
    self.mox.ReplayAll()

    # we shouldn't touch username when it appears elsewhere in the url
    self.assertEqual('https://www.facebook.com/25624/posts/snarfed.org',
                     self.fb.canonicalize_syndication_url(
                       'http://www.facebook.com/25624/posts/snarfed.org'))

    # username should override inferred username
    self.fb.inferred_username = 'mr-disguise'
    self.assertEqual('https://www.facebook.com/mr-disguise/posts/444',
                     self.fb.canonicalize_syndication_url(
                       'https://www.facebook.com/mr-disguise/posts/444'))

    # if no username, fall through
    self.fb.username = None
    self.assertEqual('https://www.facebook.com/212038/posts/444',
                     self.fb.canonicalize_syndication_url(
                       'https://www.facebook.com/mr-disguise/posts/444'))

  def test_canonicalize_syndication_url_not_facebook(self):
    """Shouldn't try to extract id and fetch post for non-facebook.com URLs."""
    url = 'https://twitter.com/foo/status/123'
    self.assertEqual(url, self.fb.canonicalize_syndication_url(url))

  def test_canonicalize_syndication_url_with_activity(self):
    """If we pass an activity with fb_object_id, use that, don't fetch from FB."""
    obj = {'fb_object_id': 456}
    act = {'object': obj}

    for activity in obj, act:
      got = self.fb.canonicalize_syndication_url('http://facebook.com/foo/posts/123',
                                                 activity=activity)
      self.assertEqual('https://www.facebook.com/212038/posts/456', got)

  def test_photo_syndication_url(self):
    """End to end test with syndication URL with FB object id instead of post id.

    Background in https://github.com/snarfed/bridgy/issues/189
    """
    self.fb.domain_urls=['http://author/url']
    self.fb.last_hfeed_fetch = testutil.NOW
    self.fb.put()

    # Facebook API calls
    self.expect_api_call('me/feed?offset=0&limit=50', {'data': [
      gr_test_facebook.PHOTO_POST]})
    self.expect_api_call('me/photos/uploaded', {'data': [gr_test_facebook.PHOTO]})
    self.expect_api_call('me/events', {})
    self.expect_api_call('sharedposts?ids=222', {})
    self.expect_api_call('comments?filter=stream&ids=222', {})

    # posse post discovery
    self.expect_requests_get('http://author/url', """
    <html>
      <div class="h-entry">
        <a class="u-url" href="http://my.orig/post"></a>
      </div>
    </html>""")

    self.assertNotIn('222', gr_test_facebook.PHOTO_POST['id'])
    self.assertEquals('222', gr_test_facebook.PHOTO_POST['object_id'])
    self.expect_api_call('212038_222', {'id': '0', 'object_id': '222'})
    self.expect_requests_get('http://my.orig/post', """
    <html class="h-entry">
      <a class="u-syndication" href="https://www.facebook.com/photo.php?fbid=222&set=a.995695740593.2393090.212038&type=1&theater'"></a>
    </html>""")

    self.mox.ReplayAll()

    resp = tasks.application.get_response(
      '/_ah/queue/poll', method='POST', body=urllib.urlencode({
          'source_key': self.fb.key.urlsafe(),
          'last_polled': '1970-01-01-00-00-00',
          }))
    self.assertEqual(200, resp.status_int)
    for resp in models.Response.query():
      self.assertEqual(['http://my.orig/post'], resp.unsent)

  def test_on_new_syndicated_post(self):
    # username is already set
    models.SyndicatedPost.insert(self.fb, original='http://or.ig',
                                 syndication='http://facebook.com/fooey/posts/123')
    fb = self.fb.key.get()
    self.assertIsNone(fb.inferred_username)

    # url has user id, not username
    fb.username = None
    fb.put()
    models.SyndicatedPost.insert(self.fb, original='http://an.other',
                                 syndication='http://facebook.com/987/posts/123')
    self.assertIsNone(fb.key.get().inferred_username)

    # no syndication url in SyndicatedPost
    models.SyndicatedPost.insert_original_blank(self.fb, original='http://x')
    self.assertIsNone(fb.key.get().inferred_username)

    # should infer username
    self.expect_api_call('212038_123', {'id': '0', 'object_id': '123'})
    self.mox.ReplayAll()
    syndpost = models.SyndicatedPost.insert(
      self.fb, original='http://fin.al',
      syndication='http://facebook.com/fooey/posts/123')
    self.assertEquals('fooey', fb.key.get().inferred_username)
    self.assertEquals('https://www.facebook.com/212038/posts/123',
                      syndpost.syndication)

  def test_pre_put_hook(self):
    self.expect_api_call('212038_1', {'id': '0', 'object_id': '2'})
    self.expect_api_call('212038_3', {'id': '0', 'object_id': '4'})
    self.expect_api_call('212038_5', {})
    self.mox.ReplayAll()

    self.assertIsNone(self.fb.key.get().resolved_object_ids_json)

    self.fb.canonicalize_syndication_url('http://facebook.com/foo/posts/1')
    self.fb.canonicalize_syndication_url('http://facebook.com/foo/posts/3')
    self.fb.put()
    self.assertEquals(json.dumps({'1': '2', '3': '4'}),
                      self.fb.key.get().resolved_object_ids_json)

    try:
      orig = facebook.MAX_RESOLVED_OBJECT_IDS
      facebook.MAX_RESOLVED_OBJECT_IDS = 2
      self.fb.canonicalize_syndication_url('http://facebook.com/foo/posts/5')
      self.fb.put()
      # should keep the highest ids
      self.assertEquals(json.dumps({'3': '4', '5': None}),
                        self.fb.key.get().resolved_object_ids_json)
    finally:
      facebook.MAX_RESOLVED_OBJECT_IDS = orig

  def test_oauth_scopes(self):
    """Ensure that passing "feature" translates to the appropriate permission
    scopes when authing when Facebook.
    """
    for feature in 'listen', 'publish', 'listen,publish', 'publish,listen':
      redirect_uri = urllib.quote_plus(
          'http://localhost/facebook/oauth_handler?state=' + urllib.quote_plus(
            '{"feature":"' + feature + '","operation":"add"}'))

      expected_auth_url = oauth_facebook.GET_AUTH_CODE_URL % {
        'scope': ','.join(sorted(set(
          (facebook.LISTEN_SCOPES if 'listen' in feature else []) +
          (facebook.PUBLISH_SCOPES if 'publish' in feature else [])))),
        'client_id': appengine_config.FACEBOOK_APP_ID,
        'redirect_uri': redirect_uri,
      }

      resp = facebook.application.get_response(
        '/facebook/start', method='POST', body=urllib.urlencode({
          'feature': feature,
        }))

      self.assertEquals(302, resp.status_code)
      self.assertEquals(expected_auth_url, resp.headers['Location'])

  def test_disable_page(self):
    user_auth_entity = self.auth_entity
    user_auth_entity.pages_json = json.dumps([self.page])
    user_auth_entity.put()

    self.auth_entity = oauth_facebook.FacebookAuth(
      id=self.page['id'], user_json=json.dumps(self.page),
      auth_code='my_code', access_token_str='my_token')
    self.auth_entity.put()
    self.fb.auth_entity = self.auth_entity.key
    self.fb.put()

    # FacebookAuth.for_page fetches the user URL with the page's access token
    self.expect_urlopen(oauth_facebook.API_USER_URL + '?access_token=page_token',
                        json.dumps(self.page))
    self.mox.ReplayAll()

    key = self.fb.key.urlsafe()
    encoded_state = urllib.quote_plus(
      '{"feature":"listen","operation":"delete","source":"' + key + '"}')

    expected_auth_url = oauth_facebook.GET_AUTH_CODE_URL % {
      'scope': '',
      'client_id': appengine_config.FACEBOOK_APP_ID,
      'redirect_uri': urllib.quote_plus(
        'http://localhost/facebook/delete/finish?state=' + encoded_state),
    }

    resp = app.application.get_response(
      '/delete/start', method='POST', body=urllib.urlencode({
        'feature': 'listen',
        'key': key,
      }))

    self.assertEquals(302, resp.status_int)
    self.assertEquals(expected_auth_url, resp.headers['Location'])

    # when silo oauth is done, it should send us back to /SOURCE/delete/finish,
    # which would in turn redirect to the more general /delete/finish.
    resp = app.application.get_response(
      '/delete/finish?'
      + 'auth_entity=' + user_auth_entity.key.urlsafe()
      + '&state=' + encoded_state)

    self.assert_equals(302, resp.status_code)
    # listen feature has been removed
    self.assert_equals([], self.fb.key.get().features)

  def test_page_chooser(self):
    self.fb.key.delete()
    self.auth_entity.pages_json = json.dumps([self.page])
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
    fb = self.fb.key.get()
    self.assertEquals(fb.bridgy_url(handler), self.response.headers['Location'])

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

  def test_publish_person_tags(self):
    self.fb.features = ['publish']
    self.fb.domains = ['foo.com']
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
    self.expect_api_call(gr_facebook.API_FEED, {'id': '123_456'}, data=urllib.urlencode({
        'message': 'my message\n\n(Originally published at: http://foo.com/bar)',
        'tags': '555,666,777,444',
      }))
    self.mox.ReplayAll()

    resp = publish.application.get_response(
      '/publish/webmention', method='POST', body=urllib.urlencode({
        'source': 'http://foo.com/bar',
        'target': 'https://brid.gy/publish/facebook',
        'source_key': self.fb.key.urlsafe(),
      }))
    self.assertEquals(201, resp.status_int)
