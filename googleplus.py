"""Google+ source code and datastore model classes.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import json
import logging
import os

from activitystreams import googleplus as as_googleplus
from activitystreams.oauth_dropins import googleplus as oauth_googleplus
from activitystreams.source import SELF
from apiclient.errors import HttpError
import appengine_config
import models
import util

from google.appengine.ext import db
import webapp2


def handle_exception(self, e, debug):
  """Exception handler that disables the source on permission errors.
  """
  if isinstance(e, HttpError):
    if e.resp.status in (403, 404):
      logging.exception('Got %d, disabling source.', e.resp.status)
      raise models.DisableSource()
    else:
      raise


class GooglePlusPage(models.Source):
  """A Google+ profile or page.

  The key name is the user id.
  """

  DISPLAY_NAME = 'Google+'
  SHORT_NAME = 'googleplus'

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
    super(GooglePlusPage, self).__init__(*args, **kwargs)
    if self.auth_entity:
      self.as_source = as_googleplus.GooglePlus(auth_entity=self.auth_entity)

  def get_activities(self, fetch_replies=False, **kwargs):
    activities = self.as_source.get_activities(
      group_id=SELF, user_id=self.key().name(), **kwargs)[1]

    if fetch_replies:
      for activity in activities:
        _, id = util.parse_tag_uri(activity['id'])
        call = self.as_source.auth_entity.api().comments().list(
          activityId=id, maxResults=500)
        comments = call.execute(self.as_source.auth_entity.http())
        for comment in comments['items']:
          self.as_source.postprocess_comment(comment)

        activity['object']['replies']['items'] = comments['items']

    return activities

class AddGooglePlusPage(util.Handler):
  def get(self):
    auth_entity = db.get(self.request.get('auth_entity'))
    gp = GooglePlusPage.create_new(self, auth_entity=auth_entity)
    self.redirect('/?added=%s' % gp.key())


application = webapp2.WSGIApplication([
    ('/googleplus/start',
     oauth_googleplus.StartHandler.to('/googleplus/oauth2callback')),
    ('/googleplus/oauth2callback',
     oauth_googleplus.CallbackHandler.to('/googleplus/add')),
    ('/googleplus/add', AddGooglePlusPage),
    ], debug=appengine_config.DEBUG)
