"""GitHub API code and datastore model classes.
"""
import logging

from flask import request
from granary import github as gr_github
from oauth_dropins import github as oauth_github
from oauth_dropins.webutil.util import json_dumps, json_loads

from models import Source
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


class GitHub(Source):
  """A GitHub user.

  The key name is the GitHub username.
  """
  GR_CLASS = gr_github.GitHub
  OAUTH_START = oauth_github.Start
  SHORT_NAME = 'github'
  TYPE_LABELS = {
    'post': 'issue',
    'like': 'star',
  }
  BACKFEED_REQUIRES_SYNDICATION_LINK = True
  DISABLE_HTTP_CODES = Source.DISABLE_HTTP_CODES + ('403',)
  CAN_PUBLISH = True
  URL_CANONICALIZER = util.UrlCanonicalizer(domain=GR_CLASS.DOMAIN,
                                            headers=util.REQUEST_HEADERS,
                                            fragment=True)
  # This makes us backfeed issue/PR comments to previous comments on the same
  # issue/PR.
  IGNORE_SYNDICATION_LINK_FRAGMENTS = True

  @staticmethod
  def new(auth_entity=None, **kwargs):
    """Creates and returns a :class:`GitHub` for the logged in user.

    Args:
      auth_entity: :class:`oauth_dropins.github.GitHubAuth`
      kwargs: property values
    """
    user = json_loads(auth_entity.user_json)
    gr_source = gr_github.GitHub(access_token=auth_entity.access_token())
    actor = gr_source.user_to_actor(user)
    return GitHub(id=auth_entity.key_id(),
                  auth_entity=auth_entity.key,
                  name=actor.get('displayName'),
                  picture=actor.get('image', {}).get('url'),
                  url=actor.get('url'),
                  **kwargs)

  def silo_url(self):
    """Returns the GitHub account URL, e.g. https://github.com/foo."""
    return self.gr_source.user_url(self.key_id())

  def label_name(self):
    """Returns the username."""
    return self.key_id()

  def user_tag_id(self):
    """Returns this user's tag URI, eg 'tag:github.com:2013,MDQ6VXNlcjc3OD='."""
    id = json_loads(self.auth_entity.get().user_json)['id']
    return self.gr_source.tag_uri(id)

  def get_activities_response(self, *args, **kwargs):
    """Drop kwargs that granary doesn't currently support for github."""
    kwargs.update({
      'fetch_shares': None,
      'fetch_mentions': None,
    })
    return self.gr_source.get_activities_response(*args, **kwargs)


class Start():
  def post(self):
    features = util.get_required_param(self, 'feature')
    scopes = PUBLISH_SCOPES if 'publish' in features else LISTEN_SCOPES
    starter = util.oauth_starter(oauth_github.Start, feature=features
                                 ).to('/github/add', scopes=scopes)
    return starter(request, self.response).post()


class AddGitHub(oauth_github.Callback):
  def finish(self, auth_entity, state=None):
    logging.debug('finish with %s, %s', auth_entity, state)
    util.maybe_add_or_delete_source(GitHub, auth_entity, state)


# ROUTES = [
#   ('/github/start', Start),
#   ('/github/add', AddGitHub),
#   ('/github/delete/finish', oauth_github.Callback.to('/delete/finish')),
#   ('/github/publish/start', oauth_github.Start.to(
#     '/publish/github/finish', scopes=PUBLISH_SCOPES)),
# ]
