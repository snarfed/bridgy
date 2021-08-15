"""Reddit source code and datastore model classes.

"""
import logging

from flask import request
from granary import reddit as gr_reddit
from granary import source as gr_source
from oauth_dropins import reddit as oauth_reddit
from oauth_dropins.webutil.util import json_dumps, json_loads
import webapp2

import models
import util


class Reddit(models.Source):
  """A Reddit account.

  The key name is the username.
  """
  GR_CLASS = gr_reddit.Reddit
  OAUTH_START = oauth_reddit.Start
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

    # Search syntax: https://www.reddit.com/wiki/search
    url_query = ' OR '.join([f'site:"{u}" OR selftext:"{u}"' for u in urls])
    return self.get_activities(
      search_query=url_query, group_id=gr_source.SEARCH, etag=self.last_activities_etag,
      fetch_replies=False, fetch_likes=False, fetch_shares=False, count=50)

class AuthHandler():
  """Base OAuth handler class."""

  def start_oauth_flow(self, feature, operation):
    """Redirects to Reddit's OAuth endpoint to start the OAuth flow.

    Args:
      feature: 'listen' or 'publish', only 'listen' supported
    """
    features = feature.split(',') if feature else []
    for feature in features:
      if feature not in models.Source.FEATURES:
        util.error(f'Unknown feature: {feature}')

    handler = util.oauth_starter(oauth_reddit.Start, feature=feature, operation=operation).to(
      '/reddit/callback')(request, self.response)
    return handler.post()


class Callback(oauth_reddit.Callback):
  def finish(self, auth_entity, state=None):
    logging.debug('finish with %s, %s', auth_entity, state)
    util.maybe_add_or_delete_source(Reddit, auth_entity, state)


class Start(AuthHandler):
  """Custom OAuth start handler so we can use access_type=read for state=listen.
  """
  def post(self):
    return self.start_oauth_flow(util.get_required_param(self, 'feature'), request.get('operation'))


# ROUTES = [
#   ('/reddit/start', Start),
#   ('/reddit/callback', Callback),
# ]
