"""bridgy App Engine app.

Implements the Salmon protocol for Facebook comments on links to external sites.
Periodically polls Facebook for new comments and sends them to those sites via
Salmon.

http://salmon-protocol.org/

TODO design:
just list of sources and destinations, no mapping!

all of a user's sources map to all their destinations. also default sources into global receiving and destinations into auto sending, with opt out.

need base url (ie prefix) for all destinations for mapping comments.

TODO:
port to webapp2?
better exception handling
better exception printing for handlers in tests. (right now just see opaque 500
  error.)
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import logging
import os
import urllib

# need to import modules with model class definitions, e.g. facebook and
# wordpress, for template rendering.
import appengine_config
import facebook
import models
import util
import wordpress

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
      sources = facebook.FacebookPage.all().filter('owner =', user)
      dests = wordpress.WordPressSite.all().filter('owner =', user)

    msgs = self.request.params.getall('msg')
    path = os.path.join(os.path.dirname(__file__), 'templates', 'dashboard.html')
    self.response.out.write(template.render(path, locals()))


class RegisterHandler(util.Handler):
  """Registers the current user if they're not already registered.
  """
  def get(self):
    self.post()

  def post(self):
    # note that the /register handler in app.yaml is login: required
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
