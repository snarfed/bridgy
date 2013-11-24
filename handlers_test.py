"""Unit tests for handlers.py.
"""

import json

import handlers
import models
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
<a class="u-url p-name" href="http://fake.com/000">asdf</a>
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
