"""Facebook source class for backfeed via notification emails.

I already regret implementing this!

https://github.com/snarfed/bridgy/issues/854
https://cloud.google.com/appengine/docs/standard/python/mail/receiving-mail-with-mail-api

to create:
pwgen --no-capitalize --ambiguous 16 1
# copy result, it goes into EMAIL_USER below
# find facebook profile, put username into ID, profile pic URL into URL
remote_api_shell.py brid-gy
from facebook_email import FacebookEmailAccount
f = FacebookEmailAccount(id=ID, features=['email'], domain_urls=[...], domains=[...],
                         email_user=EMAIL_USER, picture=URL)
f.put()
* copy other fields from existing fb source
"""
from __future__ import unicode_literals

import json
import logging

import appengine_config
from google.appengine.ext import ndb
from google.appengine.ext.webapp.mail_handlers import InboundMailHandler
from granary import facebook as gr_facebook
from granary import source as gr_source
from oauth_dropins.webutil.models import StringIdModel
import webapp2

from facebook import FacebookPage
from models import Response
import original_post_discovery
import util


class FacebookEmail(StringIdModel):
  """Stores a Facebook notification email.

  The key id is the Message-ID header.
  """
  source = ndb.KeyProperty()
  htmls = ndb.TextProperty(repeated=True)
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
      activities = [{
        'id': activity_id,
        'url': 'https://www.facebook.com/%s/posts/%s' % (self.key.id(), activity_id),
      }]

    return gr_source.Source.make_activities_base_response(activities)

  def silo_url(self):
    return self.gr_source.user_url(self.key.id())

  def get_comment(self, id, **kwargs):
    resp = ndb.Key('Response', self.gr_source.tag_uri(id))
    email = FacebookEmail.query(FacebookEmail.response == resp).get()
    if email:
      return gr_facebook.Facebook.email_to_object(email.htmls[0])

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

    util.email_me(subject='New email from %s: %s' % (sender, subject),
                  body='Source: %s' % (source.bridgy_url(self) if source else None))

    htmls = list(body.decode() for _, body in email.bodies('text/html'))
    fbe = FacebookEmail.get_or_insert(
      message_id, source=source.key if source else None, htmls=htmls)
    logging.info('FacebookEmail created %s: %s', fbe.created, fbe.key.urlsafe())

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
    # also note that base_obj['id'] is not a tag URI, it's the raw Facebook post
    # id, eg '104790764108207'. we don't use it from activities_json much,
    # though, just in PropagateResponse.source_url(), which handles this fine.

    original_post_discovery.refetch(source)
    targets, mentions = original_post_discovery.discover(source, base_obj,
                                                         fetch_hfeed=False)
    logging.info('Got targets %s mentions %s', targets, mentions)

    resp = Response(
      id=obj['id'],
      source=source.key,
      type=Response.get_type(obj),
      response_json=json.dumps(obj),
      activities_json=[json.dumps(base_obj)],
      unsent=targets)
    resp.get_or_save(source, restart=True)

    fbe.response = resp.key
    fbe.put()


application = webapp2.WSGIApplication([
  EmailHandler.mapping(),
], debug=appengine_config.DEBUG)
