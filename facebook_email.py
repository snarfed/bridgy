"""Facebook source class for backfeed via notification emails.

I already regret implementing this!

https://github.com/snarfed/bridgy/issues/854

To create one:
key = facebook_email.FacebookEmailAccount(
  id='212038',
  features=['listen'],
).put()

to create:
* create w/user id, domain_urls, domains, features=['email']
"""
from __future__ import unicode_literals

import logging

import appengine_config
from google.appengine.ext import ndb
from granary import facebook as gr_facebook
from granary import source as gr_source
import webapp2

import models
import util


class FacebookEmail(ndb.Model):
  """Stores a Facebook notification email."""
  html = ndb.TextProperty()
  as1 = ndb.TextProperty()  # JSON


class FacebookEmailAccount(models.Source):
  """A Facebook profile or page.

  The key name is the Facebook id.
  """

  GR_CLASS = gr_facebook.Facebook
  SHORT_NAME = 'facebook-email'

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


application = webapp2.WSGIApplication([
  ('/facebook-email/render/(.+)', RenderHandler),
], debug=appengine_config.DEBUG)
