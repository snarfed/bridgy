"""Bridgy front page/dashboard.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import logging
import os
import urllib
import urlparse

# need to import modules with model class definitions, e.g. facebook and
# wordpress, for template rendering.
import appengine_config
from facebook import FacebookPage
from googleplus import GooglePlusPage
from twitter import TwitterSearch
import models
import util
from wordpress import WordPressSite

from google.appengine.api import users
from google.appengine.ext import db
from google.appengine.ext import webapp
from google.appengine.ext.webapp import template
from google.appengine.ext.webapp.util import run_wsgi_app


class DashboardHandler(util.Handler):
  def get(self):
    """Renders the dashboard.

    Args:
      msg: string, message to be displayed
    """
    # TODO: switch auth to OpenID and put in a nice selector:
    # http://code.google.com/appengine/articles/openid.html
    # http://jvance.com/pages/JQueryOpenIDPlugin.xhtml
    user = models.User.get_current_user()
    if user:
      nickname = users.get_current_user().nickname()

      logout_url = users.create_logout_url('/')
      twitter_searches = list(TwitterSearch.all().filter('owner =', user))
      sources = (list(FacebookPage.all().filter('owner =', user)) +
                 list(GooglePlusPage.all().filter('owner =', user)) +
                 twitter_searches
                 )
      for source in sources:
        source.delete_url = '/%s/delete' % source.__module__

      dests = list(WordPressSite.all().filter('owner =', user))
      for dest in dests:
        dest.favicon_url = util.favicon_for_url(dest.url)

      available_twitter_dests = [d for d in dests if d.url not in
                                 [t.url for t in twitter_searches]]

    msgs = self.request.params.getall('msg')
    path = os.path.join(os.path.dirname(__file__), 'templates', 'dashboard.html')
    self.response.out.write(template.render(path, locals()))


class RegisterHandler(util.Handler):
  """Registers the current user if they're not already registered.
  """
  def get(self):
    self.post()

  def post(self):
    user = models.User.get_or_insert_current_user(self)
    logging.info('Registered %s', user.key().name())
    self.redirect('/')


def main():
  application = webapp.WSGIApplication(
    [('/', DashboardHandler),
     ('/register', RegisterHandler),
     ],
    debug=appengine_config.DEBUG)
  run_wsgi_app(application)


if __name__ == '__main__':
  main()
