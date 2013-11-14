"""Unit tests for handlers.py.
"""

import json

from handlers import ObjectHandler
import models
from webutil import testutil
import webapp2

from google.appengine.ext import db


class ObjectHandlerTest(testutil.HandlerTest):

  def setUp(self):
    super(ObjectHandlerTest, self).setUp()
    self.source_cls = self.mox.CreateMock(db.Model)
    self.source = self.mox.CreateMock(models.Source)
    self.app = webapp2.WSGIApplication([
        ('/(.+)/(.+)', ObjectHandler.using(self.source_cls, 'get_post'))])

  def test_get_html(self):
    self.source_cls.get_by_key_name('user1').AndReturn(self.source)
    self.source.get_post('post2').AndReturn({
        'url': 'http://facebook.com/123',
        'content': 'asdf'})
    self.mox.ReplayAll()

    resp = self.app.get_response('/user1/post2')
    self.assertEqual(200, resp.status_int)
    self.assert_equals("""\
<!DOCTYPE html>
<html>
<article class="h-entry">
<span class="u-uid"></span>
<a class="u-url p-name" href="http://facebook.com/123">asdf</a>
<time class="dt-published" datetime=""></time>
<time class="dt-updated" datetime=""></time>

  <div class="e-content">
  asdf

  </div>



</article>

</html>
""", resp.body)

  def test_get_json(self):
    self.source_cls.get_by_key_name('user1').AndReturn(self.source)
    self.source.get_post('post2').AndReturn({
        'url': 'http://facebook.com/123',
        'content': 'asdf'})
    self.mox.ReplayAll()

    resp = self.app.get_response('/user1/post2?format=json')
    self.assertEqual(200, resp.status_int)
    self.assert_equals({
        'type': ['h-entry'],
         'properties': {
            'name': ['asdf'],
            'url': ['http://facebook.com/123'],
            'content': [{ 'html': 'asdf', 'value': 'asdf'}],
            },
         },
        json.loads(resp.body))

  def test_bad_user(self):
    self.source_cls.get_by_key_name('user1').AndReturn(None)
    self.mox.ReplayAll()

    resp = self.app.get_response('/user1/post2')
    self.assertEqual(400, resp.status_int)

  def test_bad_format(self):
    self.source_cls.get_by_key_name('user1').AndReturn(self.source)
    self.mox.ReplayAll()

    resp = self.app.get_response('/user1/post2?format=asdf')
    self.assertEqual(400, resp.status_int)
