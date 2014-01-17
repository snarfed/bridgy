"""Google+ source code and datastore model classes.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import json
import logging
import os

from activitystreams.source import SELF
from activitystreams import googleplus as as_googleplus
from activitystreams.oauth_dropins import googleplus as oauth_googleplus
import appengine_config
import models
import util

from google.appengine.ext import db
import webapp2


class GooglePlusPage(models.Source):
  """A Google+ profile or page.

  The key name is the user id.
  """

  AS_CLASS = as_googleplus.GooglePlus
  SHORT_NAME = 'googleplus'

  # We're currently close to the G+ API's daily limit of 10k requests per day.
  # So low! :/ Usage history:
  # QPS: https://cloud.google.com/console/project/1029605954231
  # Totals by day: https://code.google.com/apis/console/b/0/?pli=1#project:1029605954231:stats
  POLL_FREQUENCY = datetime.timedelta(minutes=10)

  type = db.StringProperty(choices=('user', 'page'))

  @staticmethod
  def new(handler, auth_entity=None):
    """Creates and returns a GooglePlusPage for the logged in user.

    Args:
      handler: the current RequestHandler
      auth_entity: oauth_dropins.googleplus.GooglePlusAuth
    """
    # Google+ Person resource
    # https://developers.google.com/+/api/latest/people#resource
    user = json.loads(auth_entity.user_json)
    type = 'user' if user.get('objectType', 'person') == 'person' else 'page'
    return GooglePlusPage(key_name=user['id'],
                          auth_entity=auth_entity,
                          url=user['url'],
                          name=user['displayName'],
                          picture=user['image']['url'],
                          type=type)

  def __init__(self, *args, **kwargs):
    """Overridden because as_googleplus.GooglePlus's ctor needs auth_entity."""
    super(GooglePlusPage, self).__init__(*args, **kwargs)
    if self.auth_entity:
      self.as_source = as_googleplus.GooglePlus(auth_entity=self.auth_entity)


class OAuthCallback(util.Handler):
  """OAuth callback handler.

  Both the add and delete flows have to share this because Google+'s
  oauth-dropin doesn't yet allow multiple callback handlers. :/
  """
  def get(self):
    auth_entity = util.get_required_param(self, 'auth_entity')
    state = self.request.get('state')
    # delete uses state, add doesn't
    if state:
      self.redirect('/delete/finish?auth_entity=%s&state=%s' % (auth_entity, state))
    else:
      auth_entity = db.get(auth_entity)
      gp = GooglePlusPage.create_new(self, auth_entity=auth_entity)
      util.added_source_redirect(self, gp)


application = webapp2.WSGIApplication([
    ('/googleplus/start',
     oauth_googleplus.StartHandler.to('/googleplus/oauth2callback')),
    ('/googleplus/oauth2callback', oauth_googleplus.CallbackHandler.to('/googleplus/add')),
    ('/googleplus/add', OAuthCallback),
    ('/googleplus/delete/start', oauth_googleplus.StartHandler.to('/googleplus/oauth2callback')),
    ], debug=appengine_config.DEBUG)
