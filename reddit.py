"""Reddit source code and datastore model classes.

"""
import logging

from granary import reddit as gr_reddit
from granary import source as gr_source
from oauth_dropins import reddit as oauth_reddit
from oauth_dropins.webutil.util import json_dumps, json_loads
import webapp2
from webob import exc

import models
import util


class Reddit(models.Source):
  """A Reddit account.

  The key name is the username.
  """
  GR_CLASS = gr_reddit.Reddit
  OAUTH_START_HANDLER = oauth_reddit.StartHandler
  SHORT_NAME = 'reddit'
  TYPE_LABELS = {
    'post': 'submission',
    'comment': 'comment',
  }
  CAN_PUBLISH = False

  @staticmethod
  def new(handler, auth_entity=None, **kwargs):
    """Creates and returns a :class:`Reddit` entity.

    Args:
      handler: the current :class:`webapp2.RequestHandler`
      auth_entity: :class:`oauth_dropins.reddit.RedditAuth`
      kwargs: property values
    """
    user = json_loads(auth_entity.user_json)
    gr_source = gr_reddit.Reddit(auth_entity.refresh_token)
    return Reddit(id=user.get('name'),
                  auth_entity=auth_entity.key,
                  url=gr_source.user_url(user.get('name')),
                  name=user.get('name'),
                  picture=user.get('icon_img'),
                  **kwargs)

  def silo_url(self):
    """Returns the Reddit account URL, e.g. https://reddit.com/user/foo."""
    return self.gr_source.user_url(self.key_id())

  def label_name(self):
    """Returns the username."""
    return self.key_id()

  def search_for_links(self):
    """Searches for activities with links to any of this source's web sites.

    Returns:
      sequence of ActivityStreams activity dicts
    """
    urls = set(util.schemeless(util.fragmentless(url), slashes=False)
               for url in self.domain_urls
               if not util.in_webmention_blocklist(util.domain_from_link(url)))
    if not urls:
      return []

    candidates = []
    for u in urls:
      candidates.extend(self.get_activities(
        search_query=u, group_id=gr_source.SEARCH, etag=self.last_activities_etag,
        fetch_replies=True, fetch_likes=False, fetch_shares=False, count=50))

    return candidates


class AuthHandler(util.Handler):
  """Base OAuth handler class."""

  def start_oauth_flow(self, feature, operation):
    """Redirects to Reddit's OAuth endpoint to start the OAuth flow.

    Args:
      feature: 'listen' or 'publish', only 'listen' supported
    """
    features = feature.split(',') if feature else []
    for feature in features:
      if feature not in models.Source.FEATURES:
        raise exc.HTTPBadRequest('Unknown feature: %s' % feature)

    handler = util.oauth_starter(oauth_reddit.StartHandler, feature=feature, operation=operation).to(
      '/reddit/callback')(self.request, self.response)
    return handler.post()


class CallbackHandler(oauth_reddit.CallbackHandler, util.Handler):
  def finish(self, auth_entity, state=None):
    logging.debug('finish with %s, %s', auth_entity, state)
    self.maybe_add_or_delete_source(Reddit, auth_entity, state)


class StartHandler(AuthHandler):
  """Custom OAuth start handler so we can use access_type=read for state=listen.
  """
  def post(self):
    return self.start_oauth_flow(util.get_required_param(self, 'feature'), self.request.get('operation'))


ROUTES = [
  ('/reddit/start', StartHandler),
  ('/reddit/callback', CallbackHandler),
]
