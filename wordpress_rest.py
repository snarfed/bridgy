"""WordPress REST API (including WordPress.com) hosted blog implementation.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import collections
import datetime
import json
import logging
import os

import appengine_config

from activitystreams.oauth_dropins import wordpress_rest as oauth_wordpress
import models
import util

from google.appengine.ext import ndb
import webapp2


FakeAsClass = collections.namedtuple('FakeAsClass', ('NAME',))


class WordPress(models.Source):
  """A WordPress blog.

  The key name is the base URL.
  """
  AS_CLASS = FakeAsClass(NAME='WordPress.com')
  SHORT_NAME = 'wordpress'

  @staticmethod
  def new(handler, auth_entity=None, **kwargs):
    """Creates and returns a WordPress for the logged in user.

    Args:
      handler: the current RequestHandler
      auth_entity: oauth_dropins.wordpress.WordPressAuth
    """
    return WordPress(id=auth_entity.key.id(),
                     auth_entity=auth_entity.key,
                     url=auth_entity.blog_url,
                     name=auth_entity.user_display_name(),
                     **kwargs)

  def _url_and_domain(self, auth_entity):
    """Returns this blog's URL and domain.

    Args:
      auth_entity: oauth_dropins.wordpress_rest.WordPressAuth

    Returns: (string url, string domain, True)
    """
    return auth_entity.blog_url, auth_entity.key.id(), True

class AddWordPress(oauth_wordpress.CallbackHandler, util.Handler):
  def finish(self, auth_entity, state=None):
    self.maybe_add_or_delete_source(WordPress, auth_entity, state)


application = webapp2.WSGIApplication([
    # OAuth scopes are set in listen.html and publish.html
    ('/wordpress/start', oauth_wordpress.StartHandler.to('/wordpress/add')),
    ('/wordpress/add', AddWordPress),
    ('/wordpress/delete/start', oauth_wordpress.CallbackHandler.to('/delete/finish')),
    ], debug=appengine_config.DEBUG)
