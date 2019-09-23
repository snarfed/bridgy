"""Unit tests for facebook_email.py.
"""
from __future__ import unicode_literals
from __future__ import absolute_import

from future import standard_library
standard_library.install_aliases()
from datetime import datetime
from email.message import Message
import logging
import json
import urllib.error, urllib.parse, urllib.request

import appengine_config

from google.appengine.api import mail
from google.appengine.ext import ndb
from granary import facebook as gr_facebook
from granary.tests.test_facebook import (
  COMMENT_EMAIL_USERNAME,
  LIKE_EMAIL,
  EMAIL_COMMENT_OBJ_USERNAME,
  EMAIL_LIKE_OBJ,
)
import webapp2

import facebook_email
from facebook_email import EmailHandler, FacebookEmail, FacebookEmailAccount
from models import Response
from . import testutil


class FacebookEmailTest(testutil.ModelsTest):

  def setUp(self):
    super(FacebookEmailTest, self).setUp()
    self.fea = FacebookEmailAccount(
      id='212038',
      email_user='abc123',
      domain_urls=['http://foo.com/'],
      domains=['foo.com'],
    )
    self.fea.put()

    self.handler = EmailHandler()
    self.handler.request = webapp2.Request.blank('/_ah/mail/abc123@foo.com')
    self.handler.response = self.response

    headers = Message()
    headers['Message-ID'] = 'SMTP-123-xyz'
    self.mail = mail.InboundEmailMessage(
      sender='other@foo.com',
      to='abc123@foo.com',
      subject='Ryan Barrett commented on your post.',
      body='plain text is useless',
      html=COMMENT_EMAIL_USERNAME,
      mime_message=headers,
    )

    gr_facebook.now_fn = lambda: datetime(1999, 1, 1)

  def test_good(self):
    self.expect_requests_get('http://foo.com/', """
    <html class="h-feed">
      <div class="h-entry">
        <a class="u-url" href="http://foo.com/post"></a>
        <a class="u-syndication" href="https://www.facebook.com/snarfed.org/posts/123"></a>
      </div>
    </html>""")
    self.mox.ReplayAll()

    self.handler.receive(self.mail)
    self.assert_equals(200, self.response.status_code)

    emails = list(FacebookEmail.query())
    self.assertEquals(1, len(emails))
    self.assert_equals('SMTP-123-xyz', emails[0].key.id())
    self.assert_equals(self.fea.key, emails[0].source)
    self.assert_equals([COMMENT_EMAIL_USERNAME], emails[0].htmls)
    resp_id = EMAIL_COMMENT_OBJ_USERNAME['id']
    self.assert_equals(ndb.Key('Response', resp_id), emails[0].response)

    expected = Response(
      id=resp_id,
      source=self.fea.key,
      type='comment',
      response_json=json.dumps(EMAIL_COMMENT_OBJ_USERNAME),
      activities_json=[json.dumps({
        'id': '123',
        'numeric_id': '123',
        'url': 'https://www.facebook.com/212038/posts/123',
        'author': {'id': 'snarfed.org'},
      })],
      unsent=['http://foo.com/post'])
    self.assert_entities_equal([expected], list(Response.query()),
                               ignore=('created', 'updated'))

    tasks = self.taskqueue_stub.GetTasks('propagate')
    self.assertEquals(1, len(tasks))
    self.assert_equals(expected.key.urlsafe(),
                       testutil.get_task_params(tasks[0])['response_key'])

    self.assert_equals(EMAIL_COMMENT_OBJ_USERNAME, self.fea.get_comment('123_789'))

  def test_user_not_found(self):
    self.handler.request = webapp2.Request.blank('/_ah/mail/nope@xyz')
    self.handler.receive(self.mail)
    self.assert_equals(404, self.response.status_code)
    self.assert_equals('No Facebook email user found with address nope@xyz',
                       self.response.body)

  def test_no_html_body(self):
    del self.mail.html
    self.handler.receive(self.mail)
    self.assert_equals(400, self.response.status_code)
    self.assert_equals('No HTML body could be parsed', self.response.body)

  def test_html_parse_failed(self):
    self.mail.html = """\
<!DOCTYPE html>
<html><body>foo</body></html>"""
    self.handler.receive(self.mail)
    self.assert_equals(400, self.response.status_code)
    self.assert_equals('No HTML body could be parsed', self.response.body)

  def test_get_comment(self):
    resp_key = ndb.Key('Response', 'tag:facebook.com,2013:xyz')
    FacebookEmail(id='_', htmls=[COMMENT_EMAIL_USERNAME], response=resp_key).put()
    self.assert_equals(EMAIL_COMMENT_OBJ_USERNAME, self.fea.get_comment('xyz'))

  def test_get_like(self):
    resp_key = ndb.Key('Response', 'tag:facebook.com,2013:xyz')
    FacebookEmail(id='_', htmls=[LIKE_EMAIL], response=resp_key).put()
    self.assert_equals(EMAIL_LIKE_OBJ, self.fea.get_like('xyz'))

  def test_get_activity_id(self):
    self.assert_equals([{
      'id': 'xyz',
      'url': 'https://www.facebook.com/212038/posts/xyz',
    }], self.fea.get_activities(activity_id='xyz'))
