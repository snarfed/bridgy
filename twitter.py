"""Twitter source code and datastore model classes.

The Twitter API is dead, and so is this code.
"""
import logging

from flask import request
from granary import twitter as gr_twitter
from granary import source as gr_source
from oauth_dropins import twitter as oauth_twitter
from oauth_dropins.webutil.flask_util import error
from oauth_dropins.webutil.util import json_dumps, json_loads

from flask_app import app
import models
import util

logger = logging.getLogger(__name__)


class Twitter(models.Source):
  """A Twitter account.

  The key name is the username.
  """
  GR_CLASS = gr_twitter.Twitter
  OAUTH_START = oauth_twitter.Start
  SHORT_NAME = 'twitter'
  TYPE_LABELS = {
    'post': 'tweet',
    'comment': '@-reply',
    'repost': 'retweet',
    'like': 'favorite',
  }
  TRANSIENT_ERROR_HTTP_CODES = ('404',)
  CAN_LISTEN = False
  CAN_PUBLISH = False
  AUTH_MODEL = oauth_twitter.TwitterAuth
  MICROPUB_TOKEN_PROPERTY = 'token_secret'
  HAS_BLOCKS = True
  URL_CANONICALIZER = gr_twitter.Twitter.URL_CANONICALIZER
  USERNAME_KEY_ID = True

  @staticmethod
  def new(auth_entity=None, **kwargs):
    """Creates and returns a :class:`Twitter` entity.

    Args:
      auth_entity (oauth_dropins.twitter.TwitterAuth)
      kwargs: property values
    """
    assert 'username' not in kwargs
    assert 'id' not in kwargs
    user = json_loads(auth_entity.user_json)
    gr_source = gr_twitter.Twitter(*auth_entity.access_token())
    actor = gr_source.user_to_actor(user)
    return Twitter(username=user['screen_name'],
                   auth_entity=auth_entity.key,
                   url=actor.get('url'),
                   name=actor.get('displayName'),
                   picture=actor.get('image', {}).get('url'),
                   **kwargs)

  def silo_url(self):
    """Returns the Twitter account URL, e.g. https://twitter.com/foo."""
    return self.gr_source.user_url(self.username)

  def label_name(self):
    """Returns the username."""
    return self.username

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
    urls = {util.schemeless(util.fragmentless(url), slashes=False)
            for url in self.domain_urls
            if not util.in_webmention_blocklist(util.domain_from_link(url))}
    if not urls:
      return []

    query = ' OR '.join(sorted(urls))
    candidates = self.get_activities(
      search_query=query, group_id=gr_source.SEARCH, etag=self.last_activities_etag,
      fetch_replies=False, fetch_likes=False, fetch_shares=False, count=50)

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

  def get_like(self, activity_user_id, activity_id, like_user_id, **kwargs):
    """Returns an ActivityStreams 'like' activity object for a favorite.

    We get Twitter favorites by scraping HTML, and we only get the first page,
    which only has 25. So, use a :class:`models.Response` in the datastore
    first, if we have one, and only re-scrape HTML as a fallback.

    Args:
      activity_user_id (str): id of the user who posted the original activity
      activity_id (str): activity id
      like_user_id (str): id of the user who liked the activity
      kwargs: passed to :meth:`granary.source.Source.get_comment`
    """
    id = self.gr_source.tag_uri(f'{activity_id}_favorited_by_{like_user_id}')
    resp = models.Response.get_by_id(id)
    if resp:
      return json_loads(resp.response_json)

  def is_private(self):
    """Returns True if this Twitter account is protected.

    * https://dev.twitter.com/rest/reference/get/users/show#highlighter_25173
    * https://support.twitter.com/articles/14016
    * https://support.twitter.com/articles/20169886
    """
    return json_loads(self.auth_entity.get().user_json).get('protected')

  def canonicalize_url(self, url, activity=None, **kwargs):
    """Normalize ``/statuses/`` to ``/status/``.

    https://github.com/snarfed/bridgy/issues/618
    """
    url = url.replace('/statuses/', '/status/')
    return super().canonicalize_url(url, **kwargs)


class Auth():
  """Base OAuth handler class."""

  def start_oauth_flow(self, feature):
    """Redirects to Twitter's OAuth endpoint to start the OAuth flow.

    Args:
      feature: ``listen`` or ``publish``
    """
    features = feature.split(',') if feature else []
    for feature in features:
      if feature not in util.FEATURES:
        error(f'Unknown feature: {feature}')

    # pass explicit 'write' instead of None for publish so that oauth-dropins
    # (and tweepy) don't use signin_with_twitter ie /authorize. this works
    # around a twitter API bug: https://dev.twitter.com/discussions/21281
    access_type = 'write' if 'publish' in features else 'read'
    view = util.oauth_starter(oauth_twitter.Start, feature=feature)(
      '/twitter/add', access_type=access_type)
    return view.dispatch_request()


class Add(oauth_twitter.Callback, Auth):
  def finish(self, auth_entity, state=None):
    util.maybe_add_or_delete_source(Twitter, auth_entity, state)


class Start(oauth_twitter.Start, Auth):
  """Custom OAuth start handler that uses ``access_type=read`` for ``state=listen``.

  Tweepy converts access_type to ``x_auth_access_type`` for Twitter's
  oauth/request_token endpoint. Details:
  https://dev.twitter.com/docs/api/1/post/oauth/request_token
  """
  def dispatch_request(self):
    return self.start_oauth_flow(request.form['feature'])


app.add_url_rule('/twitter/start', view_func=Start.as_view('twitter_start', '/twitter/add'), methods=['POST'])
app.add_url_rule('/twitter/add', view_func=Add.as_view('twitter_add', 'unused'))
app.add_url_rule('/twitter/delete/finish', view_func=oauth_twitter.Callback.as_view('twitter_delete_finish', '/delete/finish'))
app.add_url_rule('/twitter/publish/start', view_func=oauth_twitter.Start.as_view('twitter_publish_finish', '/publish/twitter/finish'), methods=['POST'])
