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
pwgen --no-capitalize --ambiguous 16 1
# copy password
remote_api_shell.py brid-gy
from facebook_email import FacebookEmailAccount
f = FacebookEmailAccount(id=ID, features=['email'], domain_urls=[...], domains=[...],
                         email_user='EMAIL_USER')
f.put()
* copy other fields from existing fb source
"""
from __future__ import unicode_literals

import copy
import json
import logging

import appengine_config
from google.appengine.ext import ndb
from google.appengine.ext.webapp.mail_handlers import InboundMailHandler
from granary import facebook as gr_facebook
from granary import source as gr_source
from oauth_dropins.webutil.models import StringIdModel
import webapp2

import models
from models import Response
import original_post_discovery
from facebook import FacebookPage


class FacebookEmail(StringIdModel):
  """Stores a Facebook notification email.

  The key id is the Message-ID header.
  """
  source = ndb.KeyProperty()
  html = ndb.TextProperty(repeated=True)
  created = ndb.DateTimeProperty(auto_now_add=True, required=True)
  response = ndb.KeyProperty()


class FacebookEmailAccount(FacebookPage):
  """A Facebook profile or page.

  The key name is the Facebook user id.
  """

  GR_CLASS = gr_facebook.Facebook
  SHORT_NAME = 'facebook-email'

  # username for the inbound email address that users forward notification
  # emails to. the address will be [email_user]@brid-gy.appspotmail.com.
  # https://cloud.google.com/appengine/docs/standard/python/mail/receiving-mail-with-mail-api
  email_user = ndb.StringProperty()

  def get_activities_response(self, **kwargs):
    activities = []

    activity_id = kwargs.get('activity_id')
    if activity_id:
      resp = Response.get_by_id(self.gr_source.tag_uri(activity_id))
      if resp:
        activities = [json.loads(resp.activities_json[0])]

    return gr_source.Source.make_activities_base_response(activities)

  def silo_url(self):
    return self.gr_source.user_url(self.key.id())

  def get_comment(self, id, **kwargs):
    resp = ndb.Key('Response', self.gr_source.tag_uri(id))
    email = FacebookEmail.query(FacebookEmail.response == resp).get()
    if email:
      return gr_facebook.Facebook.email_to_object(email.html[0])

  get_like = get_comment

  def cached_resolve_object_id(self, post_id, activity=None):
    return None

  # XXX TODO
  def is_activity_public(self, activity):
    return True


class EmailHandler(InboundMailHandler):
  """Receives forwarded Facebook notification emails.

  https://cloud.google.com/appengine/docs/standard/python/mail/receiving-mail-with-mail-api
  """
  def receive(self, email):
    addr = self.request.path.split('/')[-1]
    message_id = email.original.get('message-id').strip('<>')
    sender = getattr(email, 'sender', None)
    to = getattr(email, 'to', None)
    cc = getattr(email, 'cc', None)
    subject = getattr(email, 'subject', None)
    logging.info('Received %s from %s to %s (%s) cc %s: %s',
                 message_id, sender, to, addr, cc, subject)

    addr = self.request.path.split('/')[-1]
    user = addr.split('@')[0]
    source = FacebookEmailAccount.query(FacebookEmailAccount.email_user == user).get()
    logging.info('Source for %s is %s', user, source)

    htmls = list(body.decode() for _, body in email.bodies('text/html'))
    fbe = FacebookEmail.get_or_insert(
      message_id, source=source.key if source else None, html=htmls)
    logging.info('FacebookEmail created %s', fbe.created)

    if not source:
      self.response.status_code = 404
      self.response.write('No Facebook email user found with address %s' % addr)
      return

    for html in htmls:
      obj = gr_facebook.Facebook.email_to_object(html)
      if obj:
        break
    else:
      self.response.status_code = 400
      self.response.write('No HTML body could be parsed')
      return
    logging.info('Converted to AS1: %s', json.dumps(obj, indent=2))

    base_obj = source.gr_source.base_object(obj)
    # note that this ignores the id query param (the post's user id) and uses
    # the source object's user id instead.
    base_obj['url'] = source.canonicalize_url(base_obj['url'])
    logging.info('Starting OPD for: %s', json.dumps(base_obj, indent=2))
    targets, mentions = original_post_discovery.discover(source, base_obj)
    logging.info('Got targets %s mentions %s', targets, mentions)

    activity = copy.deepcopy(base_obj)
    activity.setdefault('id', source.gr_source.base_id(base_obj['url']))
    resp = Response(
      id=obj['id'],
      source=source.key,
      type=Response.get_type(obj),
      response_json=json.dumps(obj),
      activities_json=[json.dumps(activity)],
      unsent=targets)
    resp.get_or_save(source, restart=True)

    fbe.response = resp.key
    fbe.put()


application = webapp2.WSGIApplication([
  EmailHandler.mapping(),
], debug=appengine_config.DEBUG)
