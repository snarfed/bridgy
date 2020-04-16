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
  TRANSIENT_ERROR_HTTP_CODES = ('404',)

  @staticmethod
  def new(handler, auth_entity=None, **kwargs):
    """Creates and returns a :class:`Twitter` entity.

    Args:
      handler: the current :class:`webapp2.RequestHandler`
      auth_entity: :class:`oauth_dropins.twitter.TwitterAuth`
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
    """Returns the Twitter account URL, e.g. https://twitter.com/foo."""
    return self.gr_source.user_url(self.key_id())

  def label_name(self):
    """Returns the username."""
    return self.key_id()

  def search_for_links(self):
    """Searches for activities with links to any of this source's web sites.

    Twitter search supports OR:
    https://dev.twitter.com/rest/public/search

    ...but it only returns complete(ish) results if we strip scheme from URLs,
    ie search for example.com instead of http://example.com/, and that also
    returns false positivies, so we check that the returned tweets actually have
    matching links. https://github.com/snarfed/bridgy/issues/565

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
        search_query=query, group_id=gr_source.SEARCH, etag=self.last_activities_etag,
        fetch_replies=False, fetch_likes=False, fetch_shares=False, count=50))

    # filter out retweets and search false positives that don't actually link to us
    results = []
    for candidate in candidates:
      if candidate.get('verb') == 'share':
        continue
      obj = candidate['object']
      tags = obj.get('tags', [])
      atts = obj.get('attachments', [])
      for url in urls:
        if (any(util.schemeless(t.get('url', ''), slashes=False).startswith(url)
                for t in tags + atts)):
          results.append(candidate)
          break

    return results


class AuthHandler(util.Handler):
  """Base OAuth handler class."""

  def start_oauth_flow(self, feature):
    """Redirects to Twitter's OAuth endpoint to start the OAuth flow.

    Args:
      feature: 'listen' or 'publish'
    """
    features = feature.split(',') if feature else []
    for feature in features:
      if feature not in models.Source.FEATURES:
        raise exc.HTTPBadRequest('Unknown feature: %s' % feature)

    handler = util.oauth_starter(oauth_reddit.StartHandler, feature=feature).to(
      '/reddit/add')(self.request, self.response)
    return handler.post()


class AddReddit(oauth_reddit.CallbackHandler, util.Handler):
  def finish(self, auth_entity, state=None):
    logging.debug('finish with %s, %s', auth_entity, state)
    self.maybe_add_or_delete_source(Reddit, auth_entity, util.encode_oauth_state(state))

class StartHandler(AuthHandler):
  """Custom OAuth start handler so we can use access_type=read for state=listen.

  Tweepy converts access_type to x_auth_access_type for Twitter's
  oauth/request_token endpoint. Details:
  https://dev.twitter.com/docs/api/1/post/oauth/request_token
  """
  def post(self):
    return self.start_oauth_flow(util.get_required_param(self, 'feature'))


ROUTES = [
  ('/reddit/start', StartHandler),
  ('/reddit/add', AddReddit),
  ('/reddit/delete/finish', oauth_reddit.CallbackHandler.to('/delete/finish')),
  ('/reddit/publish/start', oauth_reddit.StartHandler.to(
    '/publish/reddit/finish')),
]
