"""Google+ source code and datastore model classes.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

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

from google.appengine.ext import db
from google.appengine.api import taskqueue
from google.appengine.api import urlfetch
from google.appengine.ext import webapp
from google.appengine.ext.webapp.util import run_wsgi_app


class GooglePlusClient(db.Model):
  """Stores the bridgy client credentials that we use with the API."""
  # TODO: unify with FacebookApp
  client_id = db.StringProperty(required=True)
  client_secret = db.StringProperty(required=True)

  # this will be cached in the runtime
  __singleton = None

  @classmethod
  def get(cls):
    if not cls.__singleton:
      # TODO: check that there's only one
      cls.__singleton = cls.all().get()
      assert cls.__singleton
    return cls.__singleton


plus_api = OAuth2Decorator(
  client_id=GooglePlusClient.get().client_id,
  client_secret=GooglePlusClient.get().client_secret,
  scope='https://www.googleapis.com/auth/plus.me',
  )
service = build("plus", "v1", http=httplib2.Http())


class GooglePlusPage(models.Source):
  """A Google+ profile or page.

  The key name is the user id.
  """

  TYPE_NAME = 'Google+'

  # full human-readable name
  name = db.StringProperty()
  pic_small = db.LinkProperty()
  type = db.StringProperty(choices=('user', 'page'))

  def display_name(self):
    """Returns name.
    """
    return self.name

  def type_display_name(self):
    return self.TYPE_NAME

  @staticmethod
  def new(plus_api, handler):
    """Creates and saves a GooglePlusPage for the logged in user.

    Args:
      plus_api: OAuth2Decorator
      handler: the current webapp.RequestHandler

    Returns: GooglePlusPage
    """
    # user is a Google+ Person resource, as a dict
    # https://developers.google.com/+/api/latest/people#resource
    user = service.people().get(userId='me').execute(plus_api.http())
    logging.debug(str(user))
    id = str(user['id'])
    existing = GooglePlusPage.get_by_key_name(id)
    page = GooglePlusPage(key_name=id,
                          owner=models.User.get_current_user(),
                          )

    if existing:
      logging.warning('Overwriting GooglePlusPage %s! Old version:\n%s' %
                      (id, page.to_xml()))
      handler.messages.append('Updated existing %s page: %s' %
                              (existing.type_display_name(), existing.display_name()))
    else:
      handler.messages.append('Added %s page: %s' %
                              (page.type_display_name(), page.display_name()))

#     # TODO: ugh, *all* of this should be transactional
#     page.save()
#     taskqueue.add(name=tasks.Poll.make_task_name(page), queue_name='poll')
    return page

  # def poll(self):
  #   dests = db.GqlQuery('SELECT * FROM %s' % HARD_CODED_DEST).fetch(100)
  #   comments = []

  #   credentials = StorageByKeyName(CredentialsModel, user.gae_user_id,
  #                                  'credentials').get()

  #   if not credentials:
  #     logging.warning('Credentials not found')
  #     self.error(403)
  #     return

  #   query = """SELECT post_fbid, time, fromid, username, object_id, text FROM comment
  #              WHERE object_id IN (SELECT link_id FROM link WHERE owner = %s)
  #              ORDER BY time DESC""" % self.key().name()
  #   comment_data = self.fql(query)

#     link_ids = set(str(c['object_id']) for c in comment_data)
#     link_data = self.fql('SELECT link_id, url FROM link WHERE link_id IN (%s)' %
#                        ','.join(link_ids))
#     links = dict((l['link_id'], l['url']) for l in link_data)

#     # TODO: cache?
#     fromids = set(str(c['fromid']) for c in comment_data)
#     profile_data = self.fql(
#       'SELECT id, name, url FROM profile WHERE id IN (%s)' % ','.join(fromids))
#     profiles = dict((p['id'], p) for p in profile_data)

#     for c in comment_data:
#       link = links[c['object_id']]
#       logging.debug('Looking for destination for link: %s' % link)

#       # TODO: move rest of method to tasks!

#       # look for destinations whose url contains this link. should be at most one.
#       # (can't use this prefix code because we want the property that's a prefix
#       # of the filter value, not vice versa.)
#       # query = db.GqlQuery(
#       #   'SELECT * FROM WordPressSite WHERE url = :1 AND url <= :2',
#       #   link, link + u'\ufffd')
#       dest = [d for d in dests if link.startswith(d.url)]
#       assert len(dest) <= 1

#       if dest:
#         dest = dest[0]
#         logging.debug('Found destination: %s' % dest.key().name())

#         fromid = c['fromid']
#         profile = profiles[fromid]
#         post_url = 'https://www.facebook.com/permalink.php?story_fbid=%s&id=%s' % (
#           c['object_id'], fromid)

#         comments.append(GooglePlusComment(
#             key_name=c['post_fbid'],
#             source=self,
#             dest=dest,
#             source_post_url=post_url,
#             dest_post_url=link,
#             author_name=profile['name'],
#             author_url=profile['url'],
#             created=datetime.datetime.utcfromtimestamp(c['time']),
#             content=c['text'],
#             fb_fromid=fromid,
#             fb_username=c['username'],
#             fb_object_id=c['object_id'],
#             ))

#     return comments


# class GooglePlusComment(models.Comment):
#   """Key name is the comment's object_id.

#   Most of the properties correspond to the columns of the content table in FQL.
#   http://developers.facebook.com/docs/reference/fql/comment/
#   """

#   # user id who wrote the comment
#   fb_fromid = db.IntegerProperty(required=True)

#   # name entered by the user when they posted the comment. usually blank,
#   # generally only populated for external users. if this is provided,
#   # fb_fromid will be 0.
#   fb_username = db.StringProperty()

#   # id of the object this comment refers to
#   fb_object_id = db.IntegerProperty(required=True)


class AddGooglePlusPage(util.Handler):
  @plus_api.oauth_required
  def get(self):
    self.post()

  @plus_api.oauth_required
  def post(self):
    GooglePlusPage.new(plus_api, self)
    self.redirect('/')


class DeleteGooglePlusPage(util.Handler):
  def post(self):
    page = GooglePlusPage.get_by_key_name(self.request.params['name'])
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
