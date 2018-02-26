"""GitHub API code and datastore model classes.
"""
import json
import logging
import urlparse

import appengine_config
from google.appengine.ext import ndb
from granary import github as gr_github
from granary import source as gr_source
from oauth_dropins import github as oauth_github
import webapp2

import models
import util


# https://developer.github.com/apps/building-oauth-apps/scopes-for-oauth-apps/
# https://github.com/dear-github/dear-github/issues/113#issuecomment-365121631
LISTEN_SCOPES = [
  'notifications',
  'public_repo',
]
PUBLISH_SCOPES = [
  'public_repo',
]


class GitHub(models.Source):
  """A GitHub user.

  The key name is the GitHub username.
  """
  GR_CLASS = gr_github.GitHub
  SHORT_NAME = 'github'
  TYPE_LABELS = {
    'post': 'issue',
    'like': 'star',
  }
  BACKFEED_REQUIRES_SYNDICATION_LINK = True

  URL_CANONICALIZER = util.UrlCanonicalizer(
    domain=GR_CLASS.DOMAIN,
    headers=util.REQUEST_HEADERS)

  @staticmethod
  def new(handler, auth_entity=None, **kwargs):
    """Creates and returns a :class:`GitHub` for the logged in user.

    Args:
      handler: the current :class:`webapp2.RequestHandler`
      auth_entity: :class:`oauth_dropins.github.GitHubAuth`
      kwargs: property values
    """
    user = json.loads(auth_entity.user_json)
    gr_source = gr_github.GitHub(access_token=auth_entity.access_token())
    actor = gr_source.user_to_actor(user)

    # temporary!
    util.email_me(subject='New Bridgy GitHub user!',
                  body=json.dumps(auth_entity.user_json, indent=2))

    return GitHub(id=auth_entity.key.id(),
                  auth_entity=auth_entity.key,
                  name=actor.get('displayName'),
                  picture=actor.get('image', {}).get('url'),
                  url=actor.get('url'),
                  **kwargs)

  def silo_url(self):
    """Returns the GitHub account URL, e.g. https://github.com/foo."""
    return self.gr_source.user_url(self.key.id())

  def label_name(self):
    """Returns the username."""
    return self.key.id()

  def get_activities_response(self, *args, **kwargs):
    """Drop kwargs that granary doesn't currently support for github."""
    kwargs.update({
      'fetch_shares': None,
      'fetch_mentions': None,
    })
    return self.gr_source.get_activities_response(*args, **kwargs)


class AddGitHub(oauth_github.CallbackHandler, util.Handler):
  def finish(self, auth_entity, state=None):
    logging.debug('finish with %s, %s', auth_entity, state)
    source = self.maybe_add_or_delete_source(GitHub, auth_entity, state)


application = webapp2.WSGIApplication([
    ('/github/start', util.oauth_starter(oauth_github.StartHandler).to(
      '/github/add', scopes=LISTEN_SCOPES)),
    ('/github/add', AddGitHub),
    ('/github/delete/finish', oauth_github.CallbackHandler.to('/delete/finish')),
    ('/github/publish/start', oauth_github.StartHandler.to(
      '/publish/github/finish', scopes=PUBLISH_SCOPES)),
], debug=appengine_config.DEBUG)
