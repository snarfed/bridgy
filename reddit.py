"""Reddit source code and datastore model classes."""
from granary import reddit as gr_reddit
from granary import source as gr_source
from oauth_dropins import reddit as oauth_reddit
from oauth_dropins.webutil.util import json_dumps, json_loads
from prawcore.exceptions import NotFound

from flask_app import app
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
  DISABLE_HTTP_CODES = ('401', '403')
  USERNAME_KEY_ID = True
  URL_CANONICALIZER = util.UrlCanonicalizer(domain=GR_CLASS.DOMAIN)

  @staticmethod
  def new(auth_entity=None, **kwargs):
    """Creates and returns a :class:`Reddit` entity.

    Args:
      auth_entity (oauth_dropins.reddit.RedditAuth):
      kwargs: property values
    """
    assert 'username' not in kwargs
    assert 'id' not in kwargs
    user = json_loads(auth_entity.user_json)
    gr_source = gr_reddit.Reddit(auth_entity.refresh_token)
    return Reddit(username=user.get('name'),
                  auth_entity=auth_entity.key,
                  url=gr_source.user_url(user.get('name')),
                  name=user.get('name'),
                  picture=user.get('icon_img'),
                  **kwargs)

  def silo_url(self):
    """Returns the Reddit account URL, e.g. https://reddit.com/user/foo."""
    return self.gr_source.user_url(self.username)

  def label_name(self):
    """Returns the username."""
    return self.username

  def get_activities_response(self, *args, **kwargs):
    """Set user_id manually.

    ...since Reddit sometimes (always?) 400s our calls to
    https://oauth.reddit.com/api/v1/me (via PRAW's Reddit.user.me() ).
    """
    kwargs.setdefault('user_id', self.username)
    if kwargs.get('count'):
      kwargs['count'] = min(kwargs['count'], 10)

    try:
      return super().get_activities_response(*args, **kwargs)
    except NotFound:
      # this user was deleted or banned
      raise models.DisableSource()

  def search_for_links(self):
    """Searches for activities with links to any of this source's web sites.

    Returns:
      list of dict: ActivityStreams activities
    """
    urls = {util.schemeless(util.fragmentless(url), slashes=False)
            for url in self.domain_urls
            if not util.in_webmention_blocklist(util.domain_from_link(url))}
    if not urls:
      return []

    # Search syntax: https://www.reddit.com/wiki/search
    url_query = ' OR '.join(f'site:"{u}" OR selftext:"{u}"' for u in urls)
    return self.get_activities(
      search_query=url_query, group_id=gr_source.SEARCH, etag=self.last_activities_etag,
      fetch_replies=False, fetch_likes=False, fetch_shares=False, count=50)


class Callback(oauth_reddit.Callback):
  def finish(self, auth_entity, state=None):
    util.maybe_add_or_delete_source(Reddit, auth_entity, state)


app.add_url_rule('/reddit/start',
                 view_func=util.oauth_starter(oauth_reddit.Start).as_view('reddit_start', '/reddit/callback'), methods=['POST'])
app.add_url_rule('/reddit/callback',
                 view_func=Callback.as_view('reddit_callback', 'unused to_path'))
