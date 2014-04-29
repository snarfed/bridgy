"""Blogger API 2.0 hosted blog implementation.

https://developers.google.com/blogger/docs/2.0/developers_guide_protocol
https://support.google.com/blogger/answer/42064?hl=en
create comment:
https://developers.google.com/blogger/docs/2.0/developers_guide_protocol#CreatingComments
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import collections
import datetime
import json
import logging
import os
import urllib
import urlparse

import appengine_config
from appengine_config import HTTP_TIMEOUT

from activitystreams.oauth_dropins import blogger_v2 as oauth_blogger
import models
import util

from google.appengine.ext import ndb
import webapp2


class Blogger(models.Source):
  """A Blogger blog.

  The key name is the blog hostname.
  """
  AS_CLASS = collections.namedtuple('FakeAsClass', ('NAME',))(NAME='Blogger')
  SHORT_NAME = 'blogger'

  @staticmethod
  def new(handler, auth_entity=None, **kwargs):
    """Creates and returns a Blogger for the logged in user.

    Args:
      handler: the current RequestHandler
      auth_entity: oauth_dropins.blogger.BloggerV2Auth
    """
    url, domain, ok = Blogger._url_and_domain(auth_entity)
    if not ok:
      handler.messages = {'No Blogger blogs found. Please create one first!'}
      return None

    return Blogger(id=domain,
                   auth_entity=auth_entity.key,
                   url=url,
                   name=auth_entity.user_display_name(),
                   domain=domain,
                   domain_url=url,
                   **kwargs)

  @staticmethod
  def _url_and_domain(auth_entity):
    """Returns an auth entity's URL and domain.

    Args:
      auth_entity: oauth_dropins.blogger.BloggerV2Auth

    Returns: (string url, string domain, True)
    """
    # TODO: if they have multiple blogs (in the auth_entity.hostnames field),
    # let them choose which one to sign up.
    domain = next(iter(auth_entity.hostnames), None)
    if not domain:
      return None, None, False
    return 'http://%s/' % domain, domain, True

  def create_comment(self, post_url, author_name, author_url, content):
    """Creates a new comment in the source silo.

    Must be implemented by subclasses.

    Args:
      post_url: string
      author_name: string
      author_url: string
      content: string

    Returns: JSON response dict with 'id' and other fields
    """
    auth_entity = self.auth_entity.get()

    # extract the post's slug and look up its post id
    path = urlparse.urlparse(post_url).path
    if path.endswith('/'):
      path = path[:-1]
    slug = path.split('/')[-1]
    try:
      post_id = int(slug)
    except ValueError:
      url = API_POST_SLUG_URL % (auth_entity.key.id(), slug)
      resp = auth_entity.urlopen(url).read()
      post_id = json.loads(resp)['ID']

    # create the comment
    url = API_CREATE_COMMENT_URL % (auth_entity.key.id(), post_id)
    data = {'content': '<a href="%s">%s</a>: %s' % (author_url, author_name, content)}
    resp = auth_entity.urlopen(url, data=urllib.urlencode(data)).read()
    resp = json.loads(resp)
    resp['id'] = resp.pop('ID', None)
    return resp


class AddBlogger(oauth_blogger.CallbackHandler, util.Handler):
  def finish(self, auth_entity, state=None):
    self.maybe_add_or_delete_source(Blogger, auth_entity, state)


class OAuthCallback(util.Handler):
  """OAuth callback handler.

  Both the add and delete flows have to share this because Blogger's
  oauth-dropin doesn't yet allow multiple callback handlers. :/
  """
  def get(self):
    auth_entity_str_key = util.get_required_param(self, 'auth_entity')
    state = self.request.get('state')
    if not state:
      # state doesn't currently come through for Blogger. not sure why. doesn't
      # matter for now since we don't plan to implement listen or publish.
      state = 'webmentino'
    auth_entity = ndb.Key(urlsafe=auth_entity_str_key).get()
    self.maybe_add_or_delete_source(Blogger, auth_entity, state)


application = webapp2.WSGIApplication([
    ('/blogger/start', oauth_blogger.StartHandler.to('/blogger/oauth2callback')),
    ('/blogger/oauth2callback', oauth_blogger.CallbackHandler.to('/blogger/add')),
    ('/blogger/add', OAuthCallback),
    ('/blogger/delete/start', oauth_blogger.StartHandler.to('/blogger/oauth2callback')),
    ], debug=appengine_config.DEBUG)
