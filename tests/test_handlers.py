# coding=utf-8
"""Unit tests for handlers.py.
"""
from __future__ import unicode_literals
from __future__ import absolute_import

from future import standard_library
standard_library.install_aliases()
import io
import urllib.request, urllib.error, urllib.parse

import appengine_config
from mox3 import mox
from util import json_dumps, json_loads

import handlers
import models
from . import testutil
from .testutil import FakeGrSource, FakeSource


class HandlersTest(testutil.HandlerTest):

  def setUp(self):
    super(HandlersTest, self).setUp()
    self.source = testutil.FakeSource.new(
      self.handler, features=['listen'], domains=['or.ig', 'fa.ke'],
      domain_urls=['http://or.ig', 'https://fa.ke'])
    self.source.put()
    self.activities = [{
      'object': {
        'id': 'tag:fa.ke,2013:000',
        'url': 'http://fa.ke/000',
        'content': 'asdf http://other/link qwert',
        'author': {
          'id': self.source.user_tag_id(),
          'image': {'url': 'http://example.com/ryan/image'},
        },
        'tags': [{
          'id': 'tag:fa.ke,2013:nobody',
        }, {
          'id': self.source.user_tag_id(),
          'objectType': 'person',
        }],
        'upstreamDuplicates': ['http://or.ig/post'],
      }}]
    FakeGrSource.activities = self.activities
    FakeGrSource.event = {
      'object': {
        'id': 'tag:fa.ke,2013:123',
        'url': 'http://fa.ke/events/123',
        'content': 'Come to the next #Bridgy meetup http://other/link',
        'upstreamDuplicates': ['http://or.ig/event'],
      },
      'id': '123',
      'url': 'http://fa.ke/events/123',
    }

    for handler in (handlers.PostHandler, handlers.CommentHandler,
                    handlers.ReactionHandler, handlers.LikeHandler,
                    handlers.RepostHandler, handlers.RsvpHandler):
      handler.cache.clear()

  def check_response(self, url_template, expected_body=None, expected_status=200):
    # use an HTTPS request so that URL schemes are converted
    resp = handlers.application.get_response(
      url_template % self.source.key.string_id(), scheme='https')
    html = resp.body.decode('utf-8')
    self.assertEqual(expected_status, resp.status_int,
                     '%s %s' % (resp.status_int, html))

    if expected_body:
      header_lines = len(handlers.TEMPLATE.template.splitlines()) - 2
      actual = '\n'.join(html.splitlines()[header_lines:-1])
      self.assert_multiline_equals(expected_body, actual, ignore_blanks=True)

    return resp

  def test_post_html(self):
    self.check_response('/post/fake/%s/000', """\
<article class="h-entry">
<span class="p-uid">tag:fa.ke,2013:000</span>
  <span class="p-author h-card">
    <data class="p-uid" value="%(id)s"></data>
    <a class="u-url" href="http://fa.ke/%(key)s">http://fa.ke/%(key)s</a>
    <img class="u-photo" src="https://example.com/ryan/image" alt="" />
  </span>
<a class="u-url" href="http://fa.ke/000">http://fa.ke/000</a>
<a class="u-url" href="http://or.ig/post"></a>
  <div class="e-content p-name">
  asdf http://other/link qwert
  <a class="u-mention" href="http://other/link"></a>
  </div>
<span class="u-category h-card">
<data class="p-uid" value="%(id)s"></data>
<a class="u-url" href="http://or.ig">http://or.ig</a>
<a class="u-url" href="https://fa.ke"></a>
</span>
</article>
""" % {'key': self.source.key.id(), 'id': self.source.user_tag_id()})

  def test_post_json(self):
    resp = handlers.application.get_response(
      '/post/fake/%s/000?format=json' % self.source.key.string_id(), scheme='https')
    self.assertEqual(200, resp.status_int, resp.body)
    self.assert_equals({
      'type': ['h-entry'],
      'properties': {
        'uid': ['tag:fa.ke,2013:000'],
        'url': ['http://fa.ke/000', 'http://or.ig/post'],
        'content': [{ 'html': """\
asdf http://other/link qwert
<a class="u-mention" href="http://other/link"></a>
""",
                      'value': 'asdf http://other/link qwert',
        }],
        'author': [{
            'type': ['h-card'],
            'properties': {
              'uid': [self.source.user_tag_id()],
              'url': ['http://fa.ke/%s' % self.source.key.id()],
              'photo': ['https://example.com/ryan/image'],
            },
        }],
        'category': [{
          'type': ['h-card'],
          'properties': {
            'uid': [self.source.user_tag_id()],
            'url': ['http://or.ig', 'https://fa.ke'],
          },
        }],
      },
    }, json_loads(resp.body))

  def test_post_missing(self):
    FakeGrSource.activities = []
    self.check_response('/post/fake/%s/000', expected_status=404)

  def test_bad_source_type(self):
    self.check_response('/post/not_a_type/%s/000', expected_status=400)

  def test_bad_user(self):
    self.check_response('/post/fake/not_a_user_%s/000', expected_status=400)

  def test_disabled_user(self):
    self.source.status = 'disabled'
    self.source.put()
    self.check_response('/post/fake/%s/000', expected_status=400)

  def test_user_without_listen_feature(self):
    self.source.features = []
    self.source.put()
    self.check_response('/post/fake/%s/000', expected_status=400)

  def test_bad_format(self):
    self.check_response('/post/fake/%s/000?format=asdf', expected_status=400)

  def test_bad_id(self):
    for url in ('/post/fake/%s/x"1', '/comment/fake/%s/123/y(2',
                '/like/fake/%s/abc/z$3'):
      self.check_response(url, expected_status=404)

  def test_author_uid_not_tag_uri(self):
    self.activities[0]['object']['author']['id'] = 'not a tag uri'
    resp = self.check_response('/post/fake/%s/000?format=json', expected_status=200)
    props = json_loads(resp.body)['properties']['author'][0]['properties']
    self.assert_equals(['not a tag uri'], props['uid'])
    self.assertNotIn('url', props)

  def test_ignore_unknown_query_params(self):
    self.check_response('/post/fake/%s/000?target=x/y/z')

  def test_pass_through_source_errors(self):
    user_id = self.source.key.string_id()
    err = urllib.error.HTTPError('url', 410, 'Gone', {},
                            io.StringIO('Gone baby gone'))
    self.mox.StubOutWithMock(testutil.FakeSource, 'get_activities')
    testutil.FakeSource.get_activities(activity_id='000', user_id=user_id
                                      ).AndRaise(err)
    self.mox.ReplayAll()

    resp = self.check_response('/post/fake/%s/000', expected_status=410)
    self.assertEqual('text/plain; charset=utf-8', resp.headers['Content-Type'])
    self.assertEqual('FakeSource error:\nGone baby gone', resp.body)

  def test_connection_failures_504(self):
    user_id = self.source.key.string_id()
    self.mox.StubOutWithMock(testutil.FakeSource, 'get_activities')
    testutil.FakeSource.get_activities(activity_id='000', user_id=user_id
        ).AndRaise(Exception('Connection closed unexpectedly'))
    self.mox.ReplayAll()
    resp = self.check_response('/post/fake/%s/000', expected_status=504)
    self.assertEqual('FakeSource error:\nConnection closed unexpectedly', resp.body)

  def test_handle_disable_source(self):
    self.mox.StubOutWithMock(testutil.FakeSource, 'get_activities')
    testutil.FakeSource.get_activities(
      activity_id='000', user_id=self.source.key.string_id()
      ).AndRaise(models.DisableSource())
    self.mox.ReplayAll()

    resp = self.check_response('/post/fake/%s/000', expected_status=401)
    self.assertIn("Bridgy's access to your account has expired", resp.body)

  def test_handle_value_error(self):
    self.mox.StubOutWithMock(testutil.FakeSource, 'get_activities')
    testutil.FakeSource.get_activities(
      activity_id='000', user_id=self.source.key.string_id()
      ).AndRaise(ValueError('foo bar'))
    self.mox.ReplayAll()

    resp = self.check_response('/post/fake/%s/000', expected_status=400)
    self.assertIn('FakeSource error: foo bar', resp.body)

  def test_comment(self):
    FakeGrSource.comment = {
      'id': 'tag:fa.ke,2013:a1-b2.c3',  # test alphanumeric id (like G+)
      'content': 'qwert',
      'inReplyTo': [{'url': 'http://fa.ke/000'}],
      'author': {'image': {'url': 'http://example.com/ryan/image'}},
      'tags': self.activities[0]['object']['tags'],
    }

    self.check_response('/comment/fake/%s/000/a1-b2.c3', """\
<article class="h-entry">
<span class="p-uid">tag:fa.ke,2013:a1-b2.c3</span>
  <span class="p-author h-card">
    <img class="u-photo" src="https://example.com/ryan/image" alt="" />
  </span>
  <div class="e-content p-name">
  qwert
  <a class="u-mention" href="http://other/link"></a>
  </div>
<span class="u-category h-card">
<data class="p-uid" value="%s"></data>
<a class="u-url" href="http://or.ig">http://or.ig</a>
<a class="u-url" href="https://fa.ke"></a>
</span>
<a class="u-in-reply-to" href="http://fa.ke/000"></a>
<a class="u-in-reply-to" href="http://or.ig/post"></a>
</article>
""" % self.source.user_tag_id())

  def test_like(self):
    FakeGrSource.like = {
      'objectType': 'activity',
      'verb': 'like',
      'id': 'tag:fa.ke,2013:111',
      'object': {'url': 'http://example.com/original/post'},
      'author': {
        'displayName': 'Alice',
        'image': {'url': 'http://example.com/ryan/image'},
      },
    }

    resp = self.check_response('/like/fake/%s/000/111', """\
<article class="h-entry">
<span class="p-uid">tag:fa.ke,2013:111</span>
  <span class="p-author h-card">
    <span class="p-name">Alice</span>
    <img class="u-photo" src="https://example.com/ryan/image" alt="" />
  </span>
  <span class="p-name"></span>
  <div class="">
  </div>
  <a class="u-like-of" href="http://example.com/original/post"></a>
  <a class="u-like-of" href="http://or.ig/post"></a>
</article>
""")
    self.assertIn('<title>Alice</title>', resp.body)

  def test_reaction(self):
    FakeGrSource.reaction = {
      'objectType': 'activity',
      'verb': 'react',
      'id': 'tag:fa.ke,2013:000_scissors_by_111',
      'content': '✁',
      'object': {'url': 'http://example.com/original/post'},
      'author': {
        'displayName': 'Alice',
        'image': {'url': 'http://example.com/ryan/image'},
      },
    }

    self.check_response('/react/fake/%s/000/111/scissors', """\
<article class="h-entry">
<span class="p-uid">tag:fa.ke,2013:000_scissors_by_111</span>
  <span class="p-author h-card">
    <span class="p-name">Alice</span>
    <img class="u-photo" src="https://example.com/ryan/image" alt="" />
  </span>
  <div class="e-content p-name">
  ✁
  </div>
  <a class="u-in-reply-to" href="http://example.com/original/post"></a>
  <a class="u-in-reply-to" href="http://or.ig/post"></a>
</article>
""")

  def test_repost_with_syndicated_post_and_mentions(self):
    self.activities[0]['object']['content'] += ' http://another/mention'
    models.SyndicatedPost(
      parent=self.source.key,
      original='http://or.ig/post',
      syndication='http://example.com/original/post').put()

    FakeGrSource.share = {
      'objectType': 'activity',
      'verb': 'share',
      'id': 'tag:fa.ke,2013:111',
      'object': {'url': 'http://example.com/original/post'},
      'content': 'message from sharer',
      'author': {
        'id': 'tag:fa.ke,2013:reposter_id',
        'url': 'http://personal.domain/',
        'image': {'url': 'http://example.com/ryan/image'},
      },
    }

    self.check_response('/repost/fake/%s/000/111', """\
<article class="h-entry">
<span class="p-uid">tag:fa.ke,2013:111</span>
  <span class="p-author h-card">
    <data class="p-uid" value="tag:fa.ke,2013:reposter_id"></data>
    <a class="u-url" href="http://personal.domain/">http://personal.domain/</a>
    <a class="u-url" href="http://fa.ke/reposter_id"></a>
    <img class="u-photo" src="https://example.com/ryan/image" alt="" />
  </span>
  <div class="e-content p-name">
    message from sharer
  </div>
  <a class="u-repost-of" href="http://example.com/original/post"></a>
  <a class="u-repost-of" href="http://or.ig/post"></a>
</article>
""")

  def test_repost_not_found(self):
    FakeGrSource.share = None
    self.check_response('/repost/fake/%s/000/111', expected_status=404)

  def test_rsvp(self):
    FakeGrSource.rsvp = {
      'objectType': 'activity',
      'verb': 'rsvp-no',
      'id': 'tag:fa.ke,2013:111',
      'object': {'url': 'http://example.com/event'},
      'author': {
        'id': 'tag:fa.ke,2013:rsvper_id',
        'url': 'http://fa.ke/rsvper_id',  # same URL as FakeSource.user_url()
        'image': {'url': 'http://example.com/ryan/image'},
      },
    }

    self.check_response('/rsvp/fake/%s/000/111', """\
<article class="h-entry">
<span class="p-uid">tag:fa.ke,2013:111</span>
  <span class="p-author h-card">
    <data class="p-uid" value="tag:fa.ke,2013:rsvper_id"></data>
    <a class="u-url" href="http://fa.ke/rsvper_id">http://fa.ke/rsvper_id</a>
    <img class="u-photo" src="https://example.com/ryan/image" alt="" />
  </span>
  <span class="p-name"><data class="p-rsvp" value="no">is not attending.</data></span>
  <div class="">
  </div>
  <a class="u-in-reply-to" href="http://or.ig/event"></a>
  <a class="u-in-reply-to" href="http://example.com/event"></a>
</article>
""")

  def test_invite(self):
    FakeGrSource.rsvp = {
      'id': 'tag:fa.ke,2013:111',
      'objectType': 'activity',
      'verb': 'invite',
      'url': 'http://fa.ke/event',
      'actor': {
        'displayName': 'Mrs. Host',
        'url': 'http://fa.ke/host',
      },
      'object': {
        'objectType': 'person',
        'displayName': 'Ms. Guest',
        'url': 'http://fa.ke/guest',
      },
    }

    self.check_response('/rsvp/fake/%s/000/111', """\
<article class="h-entry">
<span class="p-uid">tag:fa.ke,2013:111</span>
  <span class="p-author h-card">
    <a class="p-name u-url" href="http://fa.ke/host">Mrs. Host</a>
  </span>
<a class="p-name u-url" href="http://fa.ke/event">invited</a>
  <div class="">
  <span class="p-invitee h-card">
    <a class="p-name u-url" href="http://fa.ke/guest">Ms. Guest</a>
  </span>
  </div>
  <a class="u-in-reply-to" href="http://or.ig/event"></a>
</article>
""")

  def test_granary_source_user_url_not_implemented(self):
    self.mox.StubOutWithMock(FakeGrSource, 'user_url')
    FakeGrSource.user_url('reposter_id').AndRaise(NotImplementedError())
    self.mox.ReplayAll()

    FakeGrSource.share = {
      'objectType': 'activity',
      'verb': 'share',
      'object': {'url': 'http://example.com/original/post'},
      'author': {'id': 'tag:fa.ke,2013:reposter_id'},
    }
    resp = self.check_response('/repost/fake/%s/000/111')
    self.assertIn('<data class="p-uid" value="tag:fa.ke,2013:reposter_id">', resp.text)
    self.assertNotIn('u-url', resp.text)

  def test_original_post_urls_follow_redirects(self):
    FakeGrSource.comment = {
      'content': 'qwert',
      'inReplyTo': [{'url': 'http://fa.ke/000'}],
    }

    self.expect_requests_head('https://fa.ke/000').InAnyOrder()
    self.expect_requests_head(
      'http://or.ig/post', redirected_url='http://or.ig/post/redirect').InAnyOrder()
    self.expect_requests_head(
      'http://other/link', redirected_url='http://other/link/redirect').InAnyOrder()
    self.mox.ReplayAll()

    self.check_response('/comment/fake/%s/000/111', """\
<article class="h-entry">
<span class="p-uid"></span>
  <div class="e-content p-name">
  qwert
  <a class="u-mention" href="http://other/link"></a>
  <a class="u-mention" href="http://other/link/redirect"></a>
  </div>
  <a class="u-in-reply-to" href="http://fa.ke/000"></a>
  <a class="u-in-reply-to" href="http://or.ig/post/redirect"></a>
  <a class="u-in-reply-to" href="http://or.ig/post"></a>
</article>
""")

  def test_strip_utm_query_params(self):
    self.activities[0]['object'].update({
        'content': 'asdf http://other/link?utm_source=x&utm_medium=y&a=b qwert',
        'upstreamDuplicates': ['http://or.ig/post?utm_campaign=123'],
        })
    FakeGrSource.comment = {'content': 'qwert'}
    self.check_response('/comment/fake/%s/000/111', """\
<article class="h-entry">
<span class="p-uid"></span>
  <div class="e-content p-name">
  qwert
  <a class="u-mention" href="http://other/link?a=b"></a>
  </div>
  <a class="u-in-reply-to" href="http://or.ig/post"></a>
</article>
""")

  def test_dedupe_http_and_https(self):
    self.activities[0]['object'].update({
      'content': 'X http://mention/only Y https://reply Z https://upstream '
                 'W http://all',
      'upstreamDuplicates': ['http://upstream/only',
                             'http://upstream',
                             'http://all',
                           ],
      })

    FakeGrSource.comment = {
      'inReplyTo': [{'url': 'https://reply/only'},
                    {'url': 'http://reply'},
                    {'url': 'https://all'},
                  ],
    }
    self.check_response('/comment/fake/%s/000/111', """\
<article class="h-entry">
<span class="p-uid"></span>
  <div class="e-content p-name">
  <a class="u-mention" href="http://all/"></a>
  <a class="u-mention" href="http://mention/only"></a>
  <a class="u-mention" href="http://upstream/only"></a>
  <a class="u-mention" href="https://reply/"></a>
  <a class="u-mention" href="https://upstream/"></a>
  </div>
  <a class="u-in-reply-to" href="https://reply/only"></a>
  <a class="u-in-reply-to" href="http://reply"></a>
  <a class="u-in-reply-to" href="https://all"></a>
</article>
""")

  def test_tag_without_url(self):
    self.activities[0]['object'] = {
      'id': 'tag:fa.ke,2013:000',
      'tags': [{'foo': 'bar'}],
    }
    self.check_response('/post/fake/%s/000', """\
<article class="h-entry">
<span class="p-uid">tag:fa.ke,2013:000</span>
  <span class="p-name"></span>
  <div class="">
  </div>
</article>
""")

  def test_cache(self):
    orig = self.check_response('/post/fake/%s/000')

    # should serve the cached response and not refetch
    self.mox.StubOutWithMock(FakeGrSource, 'get_activities_response')
    self.mox.ReplayAll()

    cached = self.check_response('/post/fake/%s/000')
    self.assert_multiline_equals(orig.body, cached.body)

  def test_in_blocklist(self):
    self.mox.StubOutWithMock(FakeSource, 'is_blocked')
    FakeSource.is_blocked(mox.IgnoreArg()).AndReturn(True)
    self.mox.ReplayAll()

    self.check_response('/comment/fake/%s/000/111', expected_status=410)
