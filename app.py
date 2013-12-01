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
    sources = {str(source.key()): source for source in
               itertools.chain(FacebookPage.all(), Twitter.all(),
                               GooglePlusPage.all(), Instagram.all())}

    # manually update the source we just added or deleted to workaround
    # inconsistent global queries.
    added = self.request.get('added')
    if added and added not in sources:
      sources[added] = db.get(added)
    deleted = self.request.get('deleted')
    if deleted in sources:
      del sources[deleted]

    # sort sources by name
    sources = sorted(sources.values(), key=lambda s: (s.DISPLAY_NAME, s.name))

    msgs = self.request.params.getall('msg')
    path = os.path.join(os.path.dirname(__file__), 'templates', 'dashboard.html')

    self.response.headers['Link'] = ('<%s/webmention>; rel="webmention"' %
                                     self.request.host_url)
    self.response.out.write(template.render(path, {'sources': sources, 'msgs': msgs}))


class DeleteHandler(util.Handler):
  def post(self):
    key = util.get_required_param(self, 'key')
    source = db.get(key)
    source.delete()
    # TODO: remove credentials, tasks, etc.
    msg = urllib.quote_plus('Deleted %s' % source.label())
    self.redirect('/?deleted=%s&msg=%s' % (key, msg))


application = webapp2.WSGIApplication(
  [('/', DashboardHandler),
   ('/delete', DeleteHandler),
   ] + handlers.HOST_META_ROUTES,
  debug=appengine_config.DEBUG)
