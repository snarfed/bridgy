"""Unit tests for handlers.py.
"""

import json

import handlers
import models
import mox
import testutil
import webapp2

from google.appengine.ext import db


class HandlersTest(testutil.HandlerTest):

  def setUp(self):
    super(HandlersTest, self).setUp()
    handlers.SOURCES['fake'] = testutil.FakeSource
    self.source = testutil.FakeSource.new(self.handler)
    self.source.as_source.DOMAIN = 'fake.com'
    self.source.set_activities(
      [{'object': {
            'id': 'tag:fake.com,2013:000',
            'url': 'http://fake.com/000',
            'content': 'asdf',
            }}])
    self.source.save()

  def test_get_post_html(self):
    resp = handlers.application.get_response('/post/fake/%s/000' %
                                             self.source.key().name())
    self.assertEqual(200, resp.status_int, resp.body)
    self.assert_equals("""\
<!DOCTYPE html>
<html>
<head><link rel="canonical" href="http://fake.com/000" /></head>
<article class="h-entry">
<span class="u-uid">tag:fake.com,2013:000</span>
<div class="p-name"><a class="u-url" href="http://fake.com/000">asdf</a></div>
<time class="dt-published" datetime=""></time>
<time class="dt-updated" datetime=""></time>

  <div class="e-content">
  asdf

  </div>



</article>

</html>
""", resp.body)

  def test_get_post_json(self):
    resp = handlers.application.get_response('/post/fake/%s/000?format=json' %
                                             self.source.key().name())
    self.assertEqual(200, resp.status_int, resp.body)
    self.assert_equals({
        'type': ['h-entry'],
        'properties': {
          'uid': ['tag:fake.com,2013:000'],
          'name': ['asdf'],
          'url': ['http://fake.com/000'],
          'content': [{ 'html': 'asdf', 'value': 'asdf'}],
          },
        },
        json.loads(resp.body))

  def test_post_bad_user(self):
    resp = handlers.application.get_response('/post/fake/not_a_user/000')
    self.assertEqual(400, resp.status_int)

  def test_post_bad_format(self):
    resp = handlers.application.get_response('/post/fake/%s/000?format=asdf' %
                                             self.source.key().name())
    self.assertEqual(400, resp.status_int)

  def test_get_comment_html(self):
    self.source.set_comment({
        'id': 'tag:fake.com,2013:111',
        'content': 'qwert',
        'inReplyTo': [{'url': 'http://fake.com/000'}]
        })

    resp = handlers.application.get_response('/comment/fake/%s/000/111' %
                                             self.source.key().name())
    self.assertEqual(200, resp.status_int, resp.body)
    self.assert_equals("""\
<!DOCTYPE html>
<html>
<head><link rel="canonical" href="" /></head>
<article class="h-entry">
<span class="u-uid">tag:fake.com,2013:111</span>
<div class="p-name">qwert</div>
<time class="dt-published" datetime=""></time>
<time class="dt-updated" datetime=""></time>

  <div class="e-content">
  qwert

  </div>

<a class="u-in-reply-to" href="http://fake.com/000" />

</article>

</html>
""", resp.body)

  def test_get_like_html(self):
    self.source.set_like({
        'objectType': 'activity',
        'verb': 'like',
        'id': 'tag:fake.com,2013:111',
        'object': {'url': 'http://example.com/original/post'},
        })

    resp = handlers.application.get_response('/like/fake/%s/000/111' %
                                             self.source.key().name())
    self.assertEqual(200, resp.status_int, resp.body)
    self.assert_equals("""\
<!DOCTYPE html>
<html>
<head><link rel="canonical" href="" /></head>
<article class="h-entry h-as-like">
<span class="u-uid">tag:fake.com,2013:111</span>

<time class="dt-published" datetime=""></time>
<time class="dt-updated" datetime=""></time>

  <div class="e-content">
  likes <a class="u-like" href="http://example.com/original/post">this</a>.

  </div>

</article>

</html>
""", resp.body)

  def test_get_repost_html(self):
    self.source.set_repost({
        'objectType': 'activity',
        'verb': 'share',
        'id': 'tag:fake.com,2013:111',
        'object': {'url': 'http://example.com/original/post'},
        })

    resp = handlers.application.get_response('/repost/fake/%s/000/111' %
                                             self.source.key().name())
    self.assertEqual(200, resp.status_int, resp.body)
    self.assert_equals("""\
<!DOCTYPE html>
<html>
<head><link rel="canonical" href="" /></head>
<article class="h-entry h-as-repost">
<span class="u-uid">tag:fake.com,2013:111</span>

<time class="dt-published" datetime=""></time>
<time class="dt-updated" datetime=""></time>

  <div class="e-content">
  reposts <a class="u-repost" href="http://example.com/original/post">this</a>.

  </div>

</article>

</html>
""", resp.body)

