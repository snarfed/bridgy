"""Unit tests for facebook_email.py.
"""
from __future__ import unicode_literals

import copy
import logging
import json
import urllib
import urllib2

import appengine_config

from google.appengine.api import mail
from granary.tests.test_facebook import COMMENT_EMAIL, LIKE_EMAIL
import webapp2

import facebook_email
from facebook_email import EmailHandler, FacebookEmail, FacebookEmailAccount
import testutil
import util


class FacebookEmailTest(testutil.ModelsTest):

  def setUp(self):
    super(FacebookEmailTest, self).setUp()
    self.fea = FacebookEmailAccount(id='212038', email_user='abc123')
    self.fea.put()

    self.handler = EmailHandler()
    self.handler.request = webapp2.Request.blank('/_ah/mail/from@foo.com')
    self.handler.response = self.response

    self.mail = mail.InboundEmailMessage(
      sender='other@foo.com',
      to='abc123@localhost',
      subject='Ryan Barrett commented on your post.',
      body='plain text is useless',
      html=COMMENT_EMAIL,
    )

  def test_success(self):
    self.handler.receive(self.mail)
    self.assert_equals(200, self.response.status_code)

  def test_user_not_found(self):
    self.handler.request = webapp2.Request.blank('/_ah/mail/nope@xyz')
    self.handler.receive(self.mail)
    self.assert_equals(404, self.response.status_code)
    self.assert_equals('No Facebook email user found with address nope@xyz')
