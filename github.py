"""GitHub API code and datastore model classes.
"""
import logging

from flask import request
from flask.views import View
from granary import github as gr_github
from oauth_dropins import github as oauth_github
from oauth_dropins.webutil.util import json_dumps, json_loads

from flask_app import app
from models import Source
import util

logger = logging.getLogger(__name__)

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
  URL_CANONICALIZER = util.UrlCanonicalizer(domain=GR_CLASS.DOMAIN, fragment=True)
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
    """Override/drop a few kwargs."""
    kwargs.update({
      'fetch_shares': None,
      'fetch_mentions': None,
      'count': min(10, kwargs.get('count', 0)),
    })
    return self.gr_source.get_activities_response(*args, **kwargs)


class Start(View):
  def dispatch_request(self):
    features = request.form['feature']
    scopes = PUBLISH_SCOPES if 'publish' in features else LISTEN_SCOPES
    starter = util.oauth_starter(oauth_github.Start, feature=features
                                 )('/github/add', scopes=scopes)
    return starter.dispatch_request()


class AddGitHub(oauth_github.Callback):
  def finish(self, auth_entity, state=None):
    logger.debug(f'finish with {auth_entity}, {state}')
    util.maybe_add_or_delete_source(GitHub, auth_entity, state)


app.add_url_rule('/github/start', view_func=Start.as_view('github_start'), methods=['POST'])
app.add_url_rule('/github/add', view_func=AddGitHub.as_view('github_add', 'unused'))
app.add_url_rule('/github/delete/finish', view_func=oauth_github.Callback.as_view('github_delete_finish', '/delete/finish'))
app.add_url_rule('/github/publish/start', view_func=oauth_github.Start.as_view('github_publish_start', '/publish/github/finish', scopes=PUBLISH_SCOPES), methods=['POST'])
