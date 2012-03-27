"""Google+ source code and datastore model classes.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import httplib2
import logging
import os

import appengine_config
import models
import util

from apiclient.discovery import build

# hack to prevent oauth2client from trying to cache on the filesystem.
# http://groups.google.com/group/google-appengine-python/browse_thread/thread/b48c23772dbc3334
# must be done before importing.
# import oauth2client.client
# oauth2client.client.CACHED_HTTP = httplib2.Http()
if hasattr(os, 'tempnam'):
  delattr(os, 'tempnam')

from oauth2client.appengine import CredentialsModel
from oauth2client.appengine import OAuth2Decorator
from oauth2client.appengine import StorageByKeyName

from google.appengine.api import users
from google.appengine.api import memcache
from google.appengine.ext import db
from google.appengine.ext import webapp
from google.appengine.ext.webapp.util import run_wsgi_app

HARD_CODED_DEST = 'WordPressSite'

# client id and secret aren't stored in the datastore like FacebookApp since
# it's hard to have the datastore ready in unit tests at module load time.
with open(appengine_config.GOOGLE_CLIENT_SECRET_FILE) as f:
  plus_api = OAuth2Decorator(
    client_id=appengine_config.GOOGLE_CLIENT_ID,
    client_secret=f.read().strip(),
    scope='https://www.googleapis.com/auth/plus.me',
    )


class GooglePlusService(db.Model):
  """A Google+ API service wrapper. Useful for mocking.

  Not thread safe.
  """

  http = httplib2.Http()
  # initialized in call()
  service = None

  @classmethod
  def call_with_creds(cls, gae_user_id, endpoint, **kwargs):
    """Makes a Google+ API call with a user's stored credentials.

    Args:
      gae_user_id: string, App Engine user id used to retrieve the
        CredentialsModel that stores the user credentials for this call
      endpoint: string, 'RESOURCE.METHOD', e.g. 'Activities.list'

    Returns: dict
    """
    credentials = StorageByKeyName(CredentialsModel, gae_user_id,
                                   'credentials').get()
    assert credentials, 'Credentials not found for user id %s' % gae_user_id
    return cls.call(credentials.authorize(cls.http), endpoint, **kwargs)

  @classmethod
  def call(cls, http, endpoint, **kwargs):
    """Makes a Google+ API call.

    Args:
      http: httplib2.Http instance
      endpoint: string, 'RESOURCE.METHOD', e.g. 'Activities.list'

    Returns: dict
    """
    if not cls.service:
      cls.service = build('plus', 'v1', cls.http)

    resource, method = endpoint.split('.')
    resource = resource.lower()
    fn = getattr(getattr(cls.service, resource)(), method)
    return fn(**kwargs).execute(http)


class GooglePlusPage(models.Source):
  """A Google+ profile or page.

  The key name is the user id.
  """

  TYPE_NAME = 'Google+'

  gae_user_id = db.StringProperty(required=True)
  # full human-readable name
  name = db.StringProperty()
  picture = db.LinkProperty()
  type = db.StringProperty(choices=('user', 'page'))

  def display_name(self):
    return self.name

  @staticmethod
  def new(handler, http=None):
    """Creates and returns a GooglePlusPage for the logged in user.

    Args:
      handler: the current webapp.RequestHandler
      http: httplib2.Http instance
    """
    # Google+ Person resource
    # https://developers.google.com/+/api/latest/people#resource
    person = GooglePlusService.call(http, 'people.get', userId='me')
    id = person['id']
    if person.get('objectType', 'person') == 'person':
      person['objectType'] = 'user'

    return GooglePlusPage(key_name=id,
                          gae_user_id=users.get_current_user().user_id(),
                          url=person['url'],
                          owner=models.User.get_current_user(),
                          name=person['displayName'],
                          picture = person['image']['url'],
                          type=person['objectType'],
                          )

  def get_posts(self):
    """Returns list of (activity resource, link url).

    The link url is also added to each returned activity resource in the
    'bridgy_link' JSON value.

    https://developers.google.com/+/api/latest/activies#resource
    """
    activities = GooglePlusService.call_with_creds(
      self.gae_user_id, 'activities.list', userId=self.key().name(),
      collection='public', maxResults=100)
    logging.debug('@@ received:\n%s', activities)

    activities_with_links = []
    for activity in activities['items']:
      for attach in activity['object'].get('attachments', []):
        if attach['objectType'] == 'article':
          activity['bridgy_link'] = attach['url']
          activities_with_links.append((activity, attach['url']))

    return activities_with_links

  def get_comments(self, posts):
    comments = []

    for activity, dest in posts:
      # Google+ Comment resource
      # https://developers.google.com/+/api/latest/comments#resource
      comment_resources = GooglePlusService.call_with_creds(
        self.gae_user_id, 'comments.list', activityId=activity['id'],
        maxResults=100)

      for c in comment_resources['items']:
        # parse the iso8601 formatted timestamp
        created = datetime.datetime.strptime(c['published'],
                                             '%Y-%m-%dT%H:%M:%S.%fZ')
        comments.append(GooglePlusComment(
            key_name=c['id'],
            source=self,
            dest=dest,
            source_post_url=activity['url'],
            dest_post_url=activity['bridgy_link'],
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
  @plus_api.oauth_required
  def get(self):
    self.post()

  @plus_api.oauth_required
  def post(self):
    GooglePlusPage.create_new(self, http=plus_api.http())
    self.redirect('/')


class DeleteGooglePlusPage(util.Handler):
  def post(self):
    page = GooglePlusPage.get_by_key_name(self.request.params['key_name'])
    # TODO: remove credentials, tasks, etc.
    msg = 'Deleted %s source: %s' % (page.type_display_name(),
                                     page.display_name())
    page.delete()
    self.redirect('/?msg=' + msg)


application = webapp.WSGIApplication([
    ('/googleplus/add', AddGooglePlusPage),
    ('/googleplus/delete', DeleteGooglePlusPage),
    ], debug=appengine_config.DEBUG)

def main():
  run_wsgi_app(application)


if __name__ == '__main__':
  main()
