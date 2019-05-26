"""Unit tests for facebook_email.py.
"""
from __future__ import unicode_literals

import copy
from datetime import datetime
from email.message import Message
import logging
import json
import urllib
import urllib2

import appengine_config

from google.appengine.api import mail
from granary import facebook as gr_facebook
from granary.tests.test_facebook import (
  COMMENT_EMAIL,
  LIKE_EMAIL,
  EMAIL_COMMENT_OBJ,
  EMAIL_LIKE_OBJ,
)
import webapp2

import facebook_email
from facebook_email import EmailHandler, FacebookEmail, FacebookEmailAccount
from models import Response
import testutil


class FacebookEmailTest(testutil.ModelsTest):

  def setUp(self):
    super(FacebookEmailTest, self).setUp()
    self.fea = FacebookEmailAccount(id='212038', email_user='abc123')
    self.fea.put()

    self.handler = EmailHandler()
    self.handler.request = webapp2.Request.blank('/_ah/mail/abc123@foo.com')
    self.handler.response = self.response

    headers = Message()
    headers['Message-ID'] = 'SMTP-123-xyz'
    self.mail = mail.InboundEmailMessage(
      sender='other@foo.com',
      to='abc123@localhost',
      subject='Ryan Barrett commented on your post.',
      body='plain text is useless',
      html=COMMENT_EMAIL,
      mime_message=headers,
    )

    gr_facebook.now_fn = lambda: datetime(1999, 1, 1)

  def test_success(self):
    self.handler.receive(self.mail)
    self.assert_equals(200, self.response.status_code)

    self.assert_entities_equal(
      [FacebookEmail(id='SMTP-123-xyz', source=self.fea.key, html=[COMMENT_EMAIL])],
      list(FacebookEmail.query()),
      ignore=('created',))

    resps = list(Response.query())
    expected = Response(
      id=EMAIL_COMMENT_OBJ['id'],
      type='comment',
      response_json=json.dumps(EMAIL_COMMENT_OBJ),
      unsent=[EMAIL_COMMENT_OBJ['inReplyTo'][0]['url']])
    self.assert_entities_equal([expected], resps, ignore=('created', 'updated'))

    tasks = self.taskqueue_stub.GetTasks('propagate')
    self.assertEquals(1, len(tasks))
    self.assert_equals(resps[0].key.urlsafe(),
                       testutil.get_task_params(tasks[0])['response_key'])

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
    key = FacebookEmail(id='xyz', html=[COMMENT_EMAIL]).put()
    self.assert_equals(EMAIL_COMMENT_OBJ, self.fea.get_comment('xyz'))

  def test_get_like(self):
    key = FacebookEmail(id='xyz', html=[LIKE_EMAIL]).put()
    self.assert_equals(EMAIL_LIKE_OBJ, self.fea.get_like('xyz'))
