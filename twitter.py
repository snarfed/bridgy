"""Twitter source code and datastore model classes.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import logging
import os

import appengine_config
import models
import tasks
import util

from google.appengine.api import taskqueue
from google.appengine.api import urlfetch
from google.appengine.api import users
from google.appengine.ext import db
from google.appengine.ext import webapp
from google.appengine.ext.webapp.util import run_wsgi_app

HARD_CODED_DEST = 'WordPressSite'


class TwitterSearch(models.Source):
  """A Twitter search.

  The key name is the base url to search for.
  """

  TYPE_NAME = 'Twitter'
  picture = '/static/twitter_logo.png'

  @staticmethod
  def new(properties, handler):
    """Creates and saves a TwitterSearch.

    Args:
      properties: dict
      handler: the current webapp.RequestHandler

    Returns: TwitterSearch
    """
    url = properties['url']
    existing = TwitterSearch.get_by_key_name(url)
    search = TwitterSearch(key_name=url,
                           url=url,
                           owner=models.User.get_current_user(),
                           )
    if existing:
      logging.warning('Overwriting TwitterSearch %s! Old version:\n%s' %
                      (id, search.to_xml()))
      handler.messages.append('Updated existing %s search: %s' %
                              (existing.type_display_name(), existing.display_name()))
    else:
      handler.messages.append('Added %s search: %s' %
                              (search.type_display_name(), search.display_name()))

    # TODO: ugh, *all* of this should be transactional
    search.save()
    taskqueue.add(name=tasks.Poll.make_task_name(search), queue_name='poll')
    return search

  # def poll(self):
    # url = util.reduce_url(properties['url'])
  #   # TODO: make generic and expand beyond single hard coded destination.
  #   # GQL so i don't have to import the model class definition.
  #   dests = db.GqlQuery('SELECT * FROM %s' % HARD_CODED_DEST).fetch(100)
  #   comments = []

  #   # Google+ Activity resource
  #   # https://developers.google.com/+/api/latest/activies#resource
  #   activities = TwitterService.call_with_creds(
  #     self.gae_user_id, 'activities.list', userId='me', collection='public',
  #     maxResults=100)

  #   # list of (link, activity) pairs
  #   links = []
  #   for activity in activities['items']:
  #     for attach in activity['object'].get('attachments', []):
  #       if attach['objectType'] == 'article':
  #         links.append((attach['url'], activity))

  #   for link, activity in links:
  #     logging.debug('Looking for destination for link: %s' % link)

  #     # look for destinations whose url contains this link. should be at most one.
  #     # (can't use a "string prefix" query because we want the property that's a
  #     # prefix of the filter value, not vice versa.)
  #     dest = [d for d in dests if link.startswith(d.url)]
  #     assert len(dest) <= 1

  #     if dest:
  #       dest = dest[0]
  #       logging.debug('Found destination: %s' % dest.key().name())

  #       # Google+ Comment resource
  #       # https://developers.google.com/+/api/latest/comments#resource
  #       comment_resources = TwitterService.call_with_creds(
  #         self.gae_user_id, 'comments.list', activityId=activity['id'],
  #         maxResults=100)

  #       for c in comment_resources['items']:
  #         before_microsecs = c['published'].split('.')[0]
  #         created = datetime.datetime.strptime(before_microsecs,
  #                                              '%Y-%m-%dT%H:%M:%S')
  #         comments.append(TwitterComment(
  #             key_name=c['id'],
  #             source=self,
  #             dest=dest,
  #             source_post_url=activity['url'],
  #             dest_post_url=link,
  #             created=created,
  #             author_name=c['actor']['displayName'],
  #             author_url=c['actor']['url'],
  #             content=c['object']['content'],
  #             user_id=c['actor']['id'],
  #             ))

  #   return comments


class TwitterComment(models.Comment):
  """Key name is the comment's id.
  """

  # user id who wrote the comment
  user_id = db.StringProperty(required=True)


class AddTwitterSearch(util.Handler):
  def post(self):
    search = TwitterSearch.new(self.request.params, self)
    self.redirect('/')


class DeleteTwitterSearch(util.Handler):
  def post(self):
    search = TwitterSearch.get_by_key_name(self.request.params['key_name'])
    # TODO: remove tasks, etc.
    msg = 'Deleted %s source: %s' % (search.type_display_name(),
                                     search.display_name())
    search.delete()
    self.redirect('/?msg=' + msg)


application = webapp.WSGIApplication([
    ('/twitter/add', AddTwitterSearch),
    ('/twitter/delete', DeleteTwitterSearch),
    ], debug=appengine_config.DEBUG)

def main():
  run_wsgi_app(application)


if __name__ == '__main__':
  main()
