"""Google+ source code and datastore model classes.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import httplib2
import logging
import os
import pickle

import appengine_config
import models
import tasks
import util

from apiclient.discovery import build
from oauth2client.appengine import CredentialsModel
from oauth2client.appengine import OAuth2Decorator
from oauth2client.appengine import StorageByKeyName

from google.appengine.api import taskqueue
from google.appengine.api import urlfetch
from google.appengine.api import users
from google.appengine.ext import db
from google.appengine.ext import webapp
from google.appengine.ext.webapp.util import run_wsgi_app

HARD_CODED_DEST = 'WordPressSite'

# client id and secret aren't stored in the datastore like FacebookApp since
# it's hard to have the  datastore ready in unit tests at module load time.
with open('oauth_client_secret') as f:
  plus_api = OAuth2Decorator(
    client_id='1029605954231.apps.googleusercontent.com',
    client_secret=f.read().strip(),
    scope='https://www.googleapis.com/auth/plus.me',
    )
http = httplib2.Http()
service = build("plus", "v1", http)


# class GooglePlusClient(db.Model):
#   """Stores the bridgy client credentials that we use with the API."""
#   # TODO: unify with FacebookApp
#   client_id = db.StringProperty(required=True)
#   client_secret = db.StringProperty(required=True)

#   # this will be cached in the runtime
#   __singleton = None

#   @classmethod
#   def get(cls):
#     if not cls.__singleton:
#       # TODO: check that there's only one
#       cls.__singleton = cls.all().get()
#       assert cls.__singleton
#     return cls.__singleton

# plus_api = OAuth2Decorator(
#   client_id=GooglePlusClient.get().client_id,
#   client_secret=GooglePlusClient.get().client_secret,
#   scope='https://www.googleapis.com/auth/plus.me',
#   )


class GooglePlusPage(models.Source):
  """A Google+ profile or page.

  The key name is the user id.
  """

  TYPE_NAME = 'Google+'

  gae_user_id = db.StringProperty(required=True)
  name = db.StringProperty()  # full human-readable name
  picture = db.LinkProperty()
  type = db.StringProperty(choices=('user', 'page'))

  def display_name(self):
    """Returns name.
    """
    return self.name

  def type_display_name(self):
    return self.TYPE_NAME

  @staticmethod
  def new(person, handler):
    """Creates and saves a GooglePlusPage for the logged in user.

    Args:
      person: dict, a Google+ Person resource:
        https://developers.google.com/+/api/latest/people#resource
      handler: the current webapp.RequestHandler

    Returns: GooglePlusPage
    """
    id = person['id']
    if person.get('objectType', 'person') == 'person':
      person['objectType'] = 'user'

    existing = GooglePlusPage.get_by_key_name(id)
    page = GooglePlusPage(key_name=id,
                          gae_user_id=users.get_current_user().user_id(),
                          url=person['url'],
                          owner=models.User.get_current_user(),
                          name=person['displayName'],
                          picture = person['image']['url'],
                          type=person['objectType'],
                          )

    if existing:
      logging.warning('Overwriting GooglePlusPage %s! Old version:\n%s' %
                      (id, page.to_xml()))
      handler.messages.append('Updated existing %s page: %s' %
                              (existing.type_display_name(), existing.display_name()))
    else:
      handler.messages.append('Added %s page: %s' %
                              (page.type_display_name(), page.display_name()))

    # TODO: ugh, *all* of this should be transactional
    page.save()
    taskqueue.add(name=tasks.Poll.make_task_name(page), queue_name='poll')
    return page

  def poll(self):
    # TODO: make generic and expand beyond single hard coded destination.
    # GQL so i don't have to import the model class definition.
    dests = db.GqlQuery('SELECT * FROM %s' % HARD_CODED_DEST).fetch(100)
    comments = []

    credentials = StorageByKeyName(CredentialsModel, self.gae_user_id,
                                   'credentials').get()
    assert credentials, 'Credentials not found for user id %s' % self.gae_user_id

    activities = service.activities().list(
      userId='me', collection='public', maxResults=100)\
        .execute(credentials.authorize(http))

    # list of (link, activity) pairs
    links = []
    for activity in activities['items']:
      for attach in activity['object'].get('attachments', []):
        if attach['objectType'] == 'article':
          links.append((attach['url'], activity))

    for link, activity in links:
      logging.debug('Looking for destination for link: %s' % link)

      # look for destinations whose url contains this link. should be at most one.
      # (can't use a "string prefix" query because we want the property that's a
      # prefix of the filter value, not vice versa.)
      dest = [d for d in dests if link.startswith(d.url)]
      assert len(dest) <= 1

      if dest:
        dest = dest[0]
        logging.debug('Found destination: %s' % dest.key().name())

        comment_resources = service.comments().list(
          activityId=activity['id'], maxResults=100)\
          .execute(credentials.authorize(http))

        for c in comment_resources['items']:
          before_microsecs = c['published'].split('.')[0]
          created = datetime.datetime.strptime(before_microsecs,
                                               '%Y-%m-%dT%H:%M:%S')
          comments.append(GooglePlusComment(
              key_name=c['id'],
              source=self,
              dest=dest,
              source_post_url=activity['url'],
              dest_post_url=link,
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
    person = service.people().get(userId='me').execute(plus_api.http())
    GooglePlusPage.new(person, self)
    self.redirect('/')


class DeleteGooglePlusPage(util.Handler):
  def post(self):
    page = GooglePlusPage.get_by_key_name(self.request.params['key_name'])
    # TODO: remove tasks, etc.
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
