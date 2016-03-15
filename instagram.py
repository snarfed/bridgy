"""Instagram API code and datastore model classes.

Example post ID and links
  id: 595990791004231349 or 595990791004231349_247678460
    (suffix is user id)
  Permalink: http://instagram.com/p/hFYnd7Nha1/
  API URL: https://api.instagram.com/v1/media/595990791004231349
  Local handler path: /post/instagram/212038/595990791004231349

Example comment ID and links
  id: 595996024371549506
  No direct API URL or permalink, as far as I can tell. :/
  API URL for all comments on that picture:
    https://api.instagram.com/v1/media/595990791004231349_247678460/comments
  Local handler path:
    /comment/instagram/212038/595990791004231349_247678460/595996024371549506
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import json

import appengine_config

from granary import instagram as gr_instagram
from oauth_dropins import instagram as oauth_instagram
from granary.source import SELF
import models
import util

import webapp2


class Instagram(models.Source):
  """An Instagram account.

  The key name is the username.
  """

  GR_CLASS = gr_instagram.Instagram
  SHORT_NAME = 'instagram'

  URL_CANONICALIZER = util.UrlCanonicalizer(
    domain=GR_CLASS.DOMAIN,
    subdomain='www',
    approve=r'https://www.instagram.com/p/[^/?]+/',
    trailing_slash=True,
    headers=util.USER_AGENT_HEADER)
    # no reject regexp; non-private Instagram post URLs just 404

  @staticmethod
  def new(handler, auth_entity=None, **kwargs):
    """Creates and returns a InstagramPage for the logged in user.

    Args:
      handler: the current RequestHandler
      auth_entity: oauth_dropins.instagram.InstagramAuth
    """
    user = json.loads(auth_entity.user_json)
    username = user['username']
    return Instagram(id=username,
                     auth_entity=auth_entity.key,
                     name=user['full_name'],
                     picture=user['profile_picture'],
                     url='http://instagram.com/' + username,
                     **kwargs)

  def silo_url(self):
    """Returns the Instagram account URL, e.g. https://instagram.com/foo."""
    return self.url

  def user_tag_id(self):
    """Returns the tag URI for this source, e.g. 'tag:instagram.com:123456'."""
    user = json.loads(self.auth_entity.get().user_json)
    return self.gr_source.tag_uri(user.get('id') or self.key.id())

  def label_name(self):
    """Returns the username."""
    return self.key.id()

  def get_activities_response(self, *args, **kwargs):
    """Discard min_id because we still want new comments/likes on old photos."""
    kwargs.setdefault('group_id', SELF)
    if 'min_id' in kwargs:
      del kwargs['min_id']
    return self.gr_source.get_activities_response(*args, **kwargs)


class OAuthCallback(oauth_instagram.CallbackHandler, util.Handler):
  """OAuth callback handler.

  The add, delete, and interactive publish flows have to share this because
  Instagram only allows a single callback URL per app. :/
  """

  def finish(self, auth_entity, state=None):
    if 'target_url' in self.decode_state_parameter(state):
      # this is an interactive publish
      return self.redirect(util.add_query_params(
        '/publish/instagram/finish',
        util.trim_nulls({'auth_entity': auth_entity.key.urlsafe(), 'state': state})))

    self.maybe_add_or_delete_source(Instagram, auth_entity, state)


class StartHandler(util.Handler):
  """Custom handler that sets OAuth scopes based on the requested
  feature(s)
  """
  def post(self):
    features = self.request.get('feature')
    features = features.split(',') if features else []
    starter = util.oauth_starter(oauth_instagram.StartHandler).to(
      '/instagram/oauth_callback',
      # http://instagram.com/developer/authentication/#scope
      scopes='likes comments' if 'publish' in features else None)
    starter(self.request, self.response).post()


application = webapp2.WSGIApplication([
    ('/instagram/start', StartHandler),
    ('/instagram/publish/start', oauth_instagram.StartHandler.to(
      '/instagram/oauth_callback')),
    ('/instagram/oauth_callback', OAuthCallback),
    ], debug=appengine_config.DEBUG)
