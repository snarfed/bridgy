"""Google+ source code and datastore model classes.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import httplib2
import json
import logging
import os

from activitystreams.oauth_dropins import googleplus as oauth_googleplus
from apiclient.errors import HttpError
import appengine_config
import models
import util

from google.appengine.api import users
from google.appengine.api import memcache
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

  TYPE_NAME = 'Google+'

  type = db.StringProperty(choices=('user', 'page'))

  def display_name(self):
    return self.name

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
                          owner=models.User.get_current_user(),
                          name=user['displayName'],
                          picture=user['image']['url'],
                          type=type)

  def get_posts(self):
    """Returns list of (activity resource, link url).

    The link url is also added to each returned activity resource in the
    'bridgy_link' JSON value.
    """
    # Google+ Activity resource
    # https://developers.google.com/+/api/latest/activies#resource
    call = self.auth_entity.api().activities().list(userId=self.key().name(),
                                                    collection='public',
                                                    maxResults=100)
    activities = call.execute(self.auth_entity.http())

    activities_with_links = []
    for activity in activities['items']:
      for attach in activity['object'].get('attachments', []):
        if attach['objectType'] == 'article':
          activity['bridgy_link'] = attach['url']
          activities_with_links.append((activity, attach['url']))

    return activities_with_links

  def get_comments(self, posts):
    comments = []

    for activity, url in posts:
      # Google+ Comment resource
      # https://developers.google.com/+/api/latest/comments#resource
      call = self.auth_entity.api().comments().list(activityId=activity['id'],
                                                    maxResults=100)
      comment_resources = call.execute(self.auth_entity.http())

      for c in comment_resources['items']:
        # parse the iso8601 formatted timestamp
        created = datetime.datetime.strptime(c['published'],
                                             '%Y-%m-%dT%H:%M:%S.%fZ')
        comments.append(GooglePlusComment(
            key_name=c['id'],
            source=self,
            source_post_url=activity['url'],
            target_url=url, #activity['bridgy_link'],
            created=created,
            author_name=c['actor']['displayName'],
            author_url=c['actor']['url'],
            content=c['object']['content'],
            user_id=c['actor']['id'],
            ))

    return comments


class GooglePlusComment(models.Comment):
  """Key name is the comment's id.

  The properties correspond to the Google+ comment resource:
  https://developers.google.com/+/api/latest/comments#resource
  """

  # user id who wrote the comment
  user_id = db.StringProperty(required=True)


class AddGooglePlusPage(util.Handler):
  def get(self):
    auth_entity = db.get(self.request.get('auth_entity'))
    GooglePlusPage.create_new(self, auth_entity=auth_entity)
    self.redirect('/')


application = webapp2.WSGIApplication([
    ('/googleplus/start',
     oauth_googleplus.StartHandler.to('/googleplus/oauth2callback')),
    ('/googleplus/oauth2callback',
     oauth_googleplus.CallbackHandler.to('/googleplus/add')),
    ('/googleplus/add', AddGooglePlusPage),
    ], debug=appengine_config.DEBUG)
