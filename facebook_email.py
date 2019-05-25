"""Facebook source class for backfeed via notification emails.

I already regret implementing this!

https://github.com/snarfed/bridgy/issues/854
https://cloud.google.com/appengine/docs/standard/python/mail/receiving-mail-with-mail-api

To create one:
key = facebook_email.FacebookEmailAccount(
  id='212038',
  features=['listen'],
).put()

to create:
* create w/user id, domain_urls, domains, features=['email']
* copy other fields from existing fb source
"""
from __future__ import unicode_literals

import logging

import appengine_config
from google.appengine.ext import ndb
from google.appengine.ext.webapp.mail_handlers import InboundMailHandler
from granary import facebook as gr_facebook
from granary import source as gr_source
import webapp2

import models
import util


class FacebookEmail(ndb.Model):
  """Stores a Facebook notification email."""
  source = ndb.KeyProperty()
  html = ndb.TextProperty()
  # as1 = ndb.TextProperty()  # JSON


class FacebookEmailAccount(models.Source):
  """A Facebook profile or page.

  The key name is the Facebook id.
  """

  GR_CLASS = gr_facebook.Facebook
  SHORT_NAME = 'facebook-email'

  # username for the inbound email address that users forward notification
  # emails to. the address will be [email_user]@brid-gy.appspotmail.com.
  # https://cloud.google.com/appengine/docs/standard/python/mail/receiving-mail-with-mail-api
  email_user = ndb.StringProperty()

  def get_activities_response(self, **kwargs):
    return gr_source.Source.make_activities_base_response([])

  def silo_url(self):
    return self.gr_source.user_url(self.key.id())


# XXX TODO: just implement get_comment() and get_like() and use handlers.py instead?
class RenderHandler(util.Handler):
  """Renders a stored FacebookEmail as HTML with microformats2."""

  def get(self, id):
    email = FacebookEmail.get_by_id(id)
    if not email:
      self.abort(404, 'No FacebookEmail found with id %s', id)

    # TODO: render


class EmailHandler(InboundMailHandler):
  """Receives forwarded Facebook notification emails.

  https://cloud.google.com/appengine/docs/standard/python/mail/receiving-mail-with-mail-api
  """
  def receive(self, email):
    addr = self.request.path.split('/')[-1]
    sender = getattr(email, 'sender', None)
    to = getattr(email, 'to', None)
    cc = getattr(email, 'cc', None)
    subject = getattr(email, 'subject', None)
    logging.info('Received email from %s (%s) to %s cc %s: %s',
                 addr, sender, to, cc, subject)

    addr = self.request.path.split('/')[-1]
    user = addr.split('@')[0]
    source = FacebookEmailAccount.query(FacebookEmailAccount.email_user == user).get()
    logging.info('Source for %s is %s', user, source)

    if not source:
      self.response.status_code = 404
      self.response.write('No Facebook email user found with address %s' % addr)
      return

    for content_type, body in email.bodies('text/html'):
      html = body.decode()
      fbe = FacebookEmail(source=source.key, html=html).put()
      logging.info('Stored FacebookEmail %s', fbe)
      break


application = webapp2.WSGIApplication([
  ('/facebook-email/render/(.+)', RenderHandler),
  EmailHandler.mapping(),
], debug=appengine_config.DEBUG)
