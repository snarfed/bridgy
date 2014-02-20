"""Unit tests for handlers.py.
"""

import json

import handlers
import models
import mox
import testutil
import util
import webapp2

from google.appengine.ext import ndb


class HandlersTest(testutil.HandlerTest):

  def setUp(self):
    super(HandlersTest, self).setUp()
    handlers.SOURCES['fake'] = testutil.FakeSource

    self.source = testutil.FakeSource.new(self.handler)
    self.source.as_source.DOMAIN = 'fa.ke'
    self.source.set_activities(
      [{'object': {
            'id': 'tag:fa.ke,2013:000',
            'url': 'http://fa.ke/000',
            'content': 'asdf http://orig/post qwert',
            'author': {'image': {'url': 'http://example.com/ryan/image'}},
            }}])
    self.source.put()

  def check_response(self, url_template, expected):
    # use an HTTPS request so that URL schemes are converted
    resp = handlers.application.get_response(
      url_template % self.source.key.string_id(), scheme='https')
    self.assertEqual(200, resp.status_int, resp.body)
    header_lines = len(handlers.TEMPLATE.template.splitlines()) - 2
    actual = '\n'.join(resp.body.splitlines()[header_lines:-1])
    self.assert_equals(expected, actual)

  def test_post_html(self):
    self.check_response('/post/fake/%s/000', """\
<article class="h-entry">
<span class="u-uid">tag:fa.ke,2013:000</span>
<div class="p-name"><a class="u-url" href="http://fa.ke/000">asdf http://orig/post qwert</a></div>

  <div class="h-card p-author">

    <img class="u-photo" src="https://example.com/ryan/image" alt="" />
    <span class="u-uid"></span>
  </div>

  <div class="e-content">
  asdf http://orig/post qwert
  </div>

</article>
""")

  def test_post_json(self):
    resp = handlers.application.get_response(
      '/post/fake/%s/000?format=json' % self.source.key.string_id(), scheme='https')
    self.assertEqual(200, resp.status_int, resp.body)
    self.assert_equals({
        'type': ['h-entry'],
        'properties': {
          'uid': ['tag:fa.ke,2013:000'],
          'name': ['asdf http://orig/post qwert'],
          'url': ['http://fa.ke/000'],
          'content': [{ 'html': 'asdf http://orig/post qwert',
                        'value': 'asdf http://orig/post qwert',
                        }],
          'author': [{
              'type': ['h-card'],
              'properties': {'photo': ['https://example.com/ryan/image']},
              }],
          },
        }, json.loads(resp.body))

  def test_bad_source_type(self):
    resp = handlers.application.get_response('/post/not_a_type/%s/000' %
                                             self.source.key.string_id())
    self.assertEqual(400, resp.status_int)

  def test_bad_user(self):
    resp = handlers.application.get_response('/post/fake/not_a_user/000')
    self.assertEqual(400, resp.status_int)

  def test_bad_format(self):
    resp = handlers.application.get_response('/post/fake/%s/000?format=asdf' %
                                             self.source.key.string_id())
    self.assertEqual(400, resp.status_int)

  def test_bad_id(self):
    for url in ('/post/fake/%s/x-1', '/comment/fake/%s/123/y_2',
                '/like/fake/%s/abc/z@3'):
      resp = handlers.application.get_response(url % self.source.key.string_id())
      self.assertEqual(404, resp.status_int)

  def test_ignore_unknown_query_params(self):
    resp = handlers.application.get_response('/post/fake/%s/000?target=x/y/z' %
                                             self.source.key.string_id())
    self.assertEqual(200, resp.status_int)

  def test_comment(self):
    self.source.set_comment({
        'id': 'tag:fa.ke,2013:111',
        'content': 'qwert',
        'inReplyTo': [{'url': 'http://fa.ke/000'}],
        'author': {'image': {'url': 'http://example.com/ryan/image'}},
        })

    self.check_response('/comment/fake/%s/000/111', """\
<article class="h-entry">
<span class="u-uid">tag:fa.ke,2013:111</span>
<div class="p-name">qwert</div>

  <div class="h-card p-author">

    <img class="u-photo" src="https://example.com/ryan/image" alt="" />
    <span class="u-uid"></span>
  </div>

  <div class="e-content">
  qwert
  </div>

<a class="u-in-reply-to" href="http://fa.ke/000"></a>
<a class="u-in-reply-to" href="http://orig/post"></a>

</article>
""")

  def test_like(self):
    self.source.as_source.set_like({
        'objectType': 'activity',
        'verb': 'like',
        'id': 'tag:fa.ke,2013:111',
        'object': {'url': 'http://example.com/original/post'},
        'author': {'image': {'url': 'http://example.com/ryan/image'}},
        })

    self.check_response('/like/fake/%s/000/111', """\
<article class="h-entry h-as-like">
<span class="u-uid">tag:fa.ke,2013:111</span>

  <div class="h-card p-author">

    <img class="u-photo" src="https://example.com/ryan/image" alt="" />
    <span class="u-uid"></span>
  </div>

  <div class="e-content">
  likes this.

  </div>

  <a class="u-like u-like-of" href="http://example.com/original/post"></a>
  <a class="u-like u-like-of" href="http://orig/post"></a>

</article>
""")

  def test_repost(self):
    self.source.as_source.set_share({
        'objectType': 'activity',
        'verb': 'share',
        'id': 'tag:fa.ke,2013:111',
        'object': {'url': 'http://example.com/original/post'},
        'author': {'image': {'url': 'http://example.com/ryan/image'}},
        })

    self.check_response('/repost/fake/%s/000/111', """\
<article class="h-entry h-as-repost">
<span class="u-uid">tag:fa.ke,2013:111</span>

  <div class="h-card p-author">

    <img class="u-photo" src="https://example.com/ryan/image" alt="" />
    <span class="u-uid"></span>
  </div>

  <div class="e-content">
  reposts this.

  </div>

  <a class="u-repost u-repost-of" href="http://example.com/original/post"></a>
  <a class="u-repost u-repost-of" href="http://orig/post"></a>

</article>
""")

  def test_rsvp(self):
    self.source.as_source.set_rsvp({
        'objectType': 'activity',
        'verb': 'rsvp-no',
        'id': 'tag:fa.ke,2013:111',
        'object': {'url': 'http://example.com/event'},
        'author': {'image': {'url': 'http://example.com/ryan/image'}},
        'displayName': 'Alice is not attending.',
        'content': '<data class="p-rsvp" value="no">is not attending.</data>',
        })

    self.check_response('/rsvp/fake/%s/000/111', """\
<article class="h-entry h-as-rsvp">
<span class="u-uid">tag:fa.ke,2013:111</span>
<div class="p-name">Alice is not attending.</div>

  <div class="h-card p-author">

    <img class="u-photo" src="https://example.com/ryan/image" alt="" />
    <span class="u-uid"></span>
  </div>

  <div class="e-content">
  <data class="p-rsvp" value="no">is not attending.</data>
  </div>

  <a class="u-in-reply-to" href="http://orig/post"></a>
  <a class="u-in-reply-to" href="http://example.com/event"></a>

</article>
""")
