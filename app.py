"""Bridgy front page/dashboard.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import itertools
import logging
import os
import urllib
import urlparse

# need to import modules with model class definitions, e.g. facebook, for
# template rendering.
import appengine_config
from facebook import FacebookPage
from googleplus import GooglePlusPage
from instagram import Instagram
from twitter import Twitter
from twitter_search import TwitterSearch
import models
import util
from webutil import handlers

from google.appengine.api import users
from google.appengine.ext import db
from google.appengine.ext.webapp import template
import webapp2


class DashboardHandler(util.Handler):
  def get(self):
    """Renders the dashboard.

    Args:
      msg: string, message to be displayed
    """
    sources = itertools.chain(FacebookPage.all(), Twitter.all(),
                              GooglePlusPage.all(), Instagram.all())

    msgs = self.request.params.getall('msg')
    path = os.path.join(os.path.dirname(__file__), 'templates', 'dashboard.html')

    self.response.headers['Link'] = ('<%s/webmention>; rel="webmention"' %
                                     self.request.host_url)
    self.response.out.write(template.render(path, {'sources': sources, 'msgs': msgs}))


class RegisterHandler(util.Handler):
  """Registers the current user if they're not already registered.
  """
  def get(self):
    self.post()

  def post(self):
    user = models.User.get_or_insert_current_user(self)
    logging.info('Registered %s', user.key().name())
    self.redirect('/')


class DeleteHandler(util.Handler):
  def post(self):
    source = db.get(util.get_required_param(self, 'key'))
    source.delete()
    # TODO: remove credentials, tasks, etc.
    msg = 'Deleted %s' % source.label()
    self.redirect('/?msg=' + msg)


application = webapp2.WSGIApplication(
  [('/', DashboardHandler),
   ('/register', RegisterHandler),
   ('/delete', DeleteHandler),
   ] + handlers.HOST_META_ROUTES,
  debug=appengine_config.DEBUG)
