"""Facebook API code and datastore model classes.

TODO: use third_party_id if we ever need to store an FB user id anywhere else.

Example post ID and links
  id: 212038_10100823411129293  [USER-ID]_[POST-ID]
  API URL: https://graph.facebook.com/212038_10100823411094363
  Permalinks:
    https://www.facebook.com/10100823411094363
    https://www.facebook.com/212038/posts/10100823411094363
    https://www.facebook.com/photo.php?fbid=10100823411094363
  Local handler path: /post/facebook/212038/10100823411094363

Example comment ID and links
  id: 10100823411094363_10069288  [POST-ID]_[COMMENT-ID]
  API URL: https://graph.facebook.com/10100823411094363_10069288
  Permalink: https://www.facebook.com/10100823411094363&comment_id=10069288
  Local handler path: /comment/facebook/212038/10100823411094363_10069288
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import heapq
import json
import logging
import re
import sys
import urllib2
import urlparse

import appengine_config

from granary import facebook as gr_facebook
from oauth_dropins import facebook as oauth_facebook
from granary.source import SELF
import models
import util

from google.appengine.ext import ndb
from google.appengine.ext.webapp import template
import webapp2

# https://developers.facebook.com/docs/reference/login/
LISTEN_SCOPES = [
  'user_website', 'user_status', 'user_posts', 'user_photos', 'user_events',
  'user_actions.news', 'read_stream', 'manage_pages',
]
PUBLISH_SCOPES = [
  'user_website', 'publish_actions', 'rsvp_event', 'user_status',
  'user_photos', 'user_videos', 'user_events', 'user_likes',
]

# WARNING: this edge is deprecated in API v2.4 and will stop working in 2017.
# https://developers.facebook.com/docs/apps/changelog#v2_4_deprecations
API_EVENT_RSVPS = '%s/invited'

# https://developers.facebook.com/docs/graph-api/using-graph-api/#errors
DEAD_TOKEN_ERROR_CODES = frozenset((
  200,  # "Permissions error"
))
DEAD_TOKEN_ERROR_SUBCODES = frozenset((
  458,  # "The user has not authorized application 123"
  460,  # "The session has been invalidated because the user has changed the password"
))
DEAD_TOKEN_ERROR_MESSAGES = frozenset((
  'The user must be an administrator of the page in order to impersonate it.',
))

MAX_RESOLVED_OBJECT_IDS = 200


class FacebookPage(models.Source):
  """A facebook profile or page.

  The key name is the facebook id.
  """

  GR_CLASS = gr_facebook.Facebook
  SHORT_NAME = 'facebook'

  # unique name used in fb URLs, e.g. facebook.com/[username]
  username = ndb.StringProperty()
  # inferred from syndication URLs if username isn't available
  inferred_username = ndb.StringProperty()
  # maps string post ids to string facebook object ids or None. background:
  # https://github.com/snarfed/bridgy/pull/513#issuecomment-149312879
  resolved_object_ids_json = ndb.TextProperty(compressed=True)

  @staticmethod
  def new(handler, auth_entity=None, **kwargs):
    """Creates and returns a FacebookPage for the logged in user.

    Args:
      handler: the current RequestHandler
      auth_entity: oauth_dropins.facebook.FacebookAuth
      kwargs: property values
    """
    user = json.loads(auth_entity.user_json)
    gr_source = gr_facebook.Facebook(auth_entity.access_token())
    actor = gr_source.user_to_actor(user)
    return FacebookPage(id=user['id'],
                        auth_entity=auth_entity.key,
                        name=actor.get('displayName'),
                        username=actor.get('username'),
                        picture=actor.get('image', {}).get('url'),
                        url=actor.get('url'),
                        **kwargs)

  @classmethod
  def lookup(cls, id):
    """Returns the entity with the given id or username."""
    return ndb.Key(cls, id).get() or cls.query(cls.username == id).get()

  def silo_url(self):
    """Returns the Facebook account URL, e.g. https://facebook.com/foo."""
    return self.gr_source.user_url(self.username or self.key.id())

  def get_activities_response(self, **kwargs):
    kwargs.setdefault('fetch_events', True)
    kwargs.setdefault('fetch_news', self.auth_entity.get().type == 'user')
    kwargs.setdefault('event_owner_id', self.key.id())

    try:
      return super(FacebookPage, self).get_activities_response(**kwargs)
    except urllib2.HTTPError as e:
      code, body = util.interpret_http_exception(e)
      # use a function so any new exceptions (JSON decoding, missing keys) don't
      # clobber the original exception so we can re-raise it below.
      def dead_token():
        try:
          err = json.loads(body)['error']
          return (err.get('code') in DEAD_TOKEN_ERROR_CODES or
                  err.get('error_subcode') in DEAD_TOKEN_ERROR_SUBCODES or
                  err.get('message') in DEAD_TOKEN_ERROR_MESSAGES)
        except:
          logging.exception("Couldn't determine whether token is still valid")
          return False

      if code == '401':
        if not dead_token():
          # ask the user to reauthenticate. if this API call fails, it will raise
          # urllib2.HTTPError instead of DisableSource, so that we don't disable
          # the source without notifying.
          self.gr_source.create_notification(
            self.key.id(),
            "Brid.gy's access to your account has expired. Click here to renew it now!",
            'https://brid.gy/facebook/start')
        raise models.DisableSource()

      raise

  def canonicalize_syndication_url(self, url, activity=None, **kwargs):
    """Facebook-specific standardization of syndicated urls. Canonical form is
    https://www.facebook.com/USERID/posts/POSTID

    Args:
      url: a string, the url of the syndicated content
      activity: the activity this URL came from. If it has an fb_object_id,
        we'll use that instead of fetching the post from Facebook
      kwargs: unused

    Return:
      a string, the canonical form of the syndication url
    """
    if util.domain_from_link(url) != self.gr_source.DOMAIN:
      return url

    def post_url(id):
      return 'https://www.facebook.com/%s/posts/%s' % (self.key.id(), id)

    parsed = urlparse.urlparse(url)
    params = urlparse.parse_qs(parsed.query)
    url_id = self.gr_source.post_id(url)

    ids = params.get('story_fbid') or params.get('fbid')
    if ids:
      url = post_url(ids[0])
    elif url_id:
      if parsed.path.startswith('/notes/'):
        url = post_url(url_id)
      else:
        object_id = self.cached_resolve_object_id(url_id, activity=activity)
        if object_id:
          url = post_url(object_id)

    username = self.username or self.inferred_username
    if username:
      url = url.replace('facebook.com/%s/' % username,
                        'facebook.com/%s/' % self.key.id())

    # facebook always uses https and www
    return super(FacebookPage, self).canonicalize_syndication_url(
      url, scheme='https', subdomain='www.')

  def cached_resolve_object_id(self, post_id, activity=None):
    """Resolve a post id to its Facebook object id, if any.

    Wraps granary.facebook.Facebook.resolve_object_id() and uses
    self.resolved_object_ids_json as a cache.

    Args:
      post_id: string Facebook post id
      activity: optional AS activity representation of Facebook post

    Returns: string Facebook object id or None
    """
    if self.updates is None:
      self.updates = {}

    parsed = gr_facebook.Facebook.parse_id(post_id)
    if parsed.post:
      post_id = parsed.post

    resolved = self.updates.setdefault('resolved_object_ids', {})
    if self.resolved_object_ids_json and not resolved:
      resolved = self.updates['resolved_object_ids'] = json.loads(
        self.resolved_object_ids_json)

    if post_id not in resolved:
      resolved[post_id] = self.gr_source.resolve_object_id(
        self.key.id(), post_id, activity=activity)

    return resolved[post_id]

  def _pre_put_hook(self):
    """Encode updates['resolved_object_ids'] into resolved_object_ids_json.

    ...and cap it at MAX_RESOLVED_OBJECT_IDS.
    """
    if self.updates:
      resolved = self.updates.get('resolved_object_ids')
      if resolved:
        keep = heapq.nlargest(
          MAX_RESOLVED_OBJECT_IDS,
          (int(id) if util.is_int(id) else id for id in resolved.keys()))
        logging.info('Saving %s resolved Facebook post ids.', len(keep))
        self.resolved_object_ids_json = json.dumps(
          {str(id): resolved[str(id)] for id in keep})

  def infer_profile_url(self, url):
    """Find a Facebook profile URL (ideally the one with the user's numeric ID)

    Looks up existing sources by username, inferred username, and domain.

    Args:
      url: string, a person's URL

    Return:
      a string URL for their Facebook profile (or None)
    """
    domain = util.domain_from_link(url)
    if domain == self.gr_source.DOMAIN:
      username = urlparse.urlparse(url).path.strip('/')
      if '/' not in username:
        user = FacebookPage.query(ndb.OR(
          FacebookPage.username == username,
          FacebookPage.inferred_username == username)).get()
        if user:
          return self.gr_source.user_url(user.key.id())
    return super(FacebookPage, self).infer_profile_url(url)

  @ndb.transactional
  def on_new_syndicated_post(self, syndpost):
    """If this source has no username, try to infer one from a syndication URL.

    Args:
      syndpost: SyndicatedPost
    """
    url = syndpost.syndication
    if self.username or not url:
      return

    # FB usernames only have letters, numbers, and periods:
    # https://www.facebook.com/help/105399436216001
    author_id = self.gr_source.base_object({'object': {'url': url}})\
                              .get('author', {}).get('id')
    if author_id and not util.is_int(author_id):
      logging.info('Inferring username %s from syndication url %s', author_id, url)
      self.inferred_username = author_id
      self.put()
      syndpost.syndication = self.canonicalize_syndication_url(syndpost.syndication)


class AuthHandler(util.Handler):
  """Base OAuth handler class."""

  def finish_oauth_flow(self, auth_entity, state):
    """Adds or deletes a FacebookPage, or restarts OAuth to get publish permissions.

    Args:
      auth_entity: FacebookAuth
      state: encoded state string
    """
    if auth_entity is None:
      auth_entity_key = self.request.get('auth_entity_key')
      if auth_entity_key:
        auth_entity = ndb.Key(urlsafe=auth_entity_key).get()

    if state is None:
      state = self.request.get('state')
    state_obj = self.decode_state_parameter(state) if state else {}

    id = state_obj.get('id') or self.request.get('id')
    if id and auth_entity and id != auth_entity.key.id():
      auth_entity = auth_entity.for_page(id)
      auth_entity.put()

    source = self.maybe_add_or_delete_source(FacebookPage, auth_entity, state)

    # If we were already signed up for publish, we had an access token with publish
    # permissions. If we then go through the listen signup flow, we'll get a token
    # with just the listen permissions. In that case, do the whole OAuth flow again
    # to get a token with publish permissions again.
    feature = state_obj.get('feature')
    if source is not None and feature == 'listen' and 'publish' in source.features:
      logging.info('Restarting OAuth flow to get publish permissions.')
      source.features.remove('publish')
      source.put()
      start = util.oauth_starter(oauth_facebook.StartHandler,
                                 feature='publish', id=id)
      restart = start.to('/facebook/oauth_handler', scopes=PUBLISH_SCOPES)
      restart(self.request, self.response).post()


class AddFacebookPage(AuthHandler):
  def post(self, auth_entity=None, state=None):
    self.finish_oauth_flow(auth_entity, state)


class OAuthCallback(oauth_facebook.CallbackHandler, AuthHandler):
  """OAuth callback handler."""
  def finish(self, auth_entity, state=None):
    id = self.decode_state_parameter(state).get('id')

    if auth_entity and json.loads(auth_entity.pages_json) and not id:
      # this user has FB page(s), and we don't know whether they want to sign
      # themselves up or one of their pages, so ask them.
      vars = {
        'action': '/facebook/add',
        'state': state,
        'auth_entity_key': auth_entity.key.urlsafe(),
        'choices': [json.loads(auth_entity.user_json)] +
                   json.loads(auth_entity.pages_json),
        }
      logging.info('Rendering choose_facebook.html with %s', vars)
      self.response.headers['Content-Type'] = 'text/html'
      self.response.out.write(
        template.render('templates/choose_facebook.html', vars))
      return

    # this user has no FB page(s), or we know the one they want to sign up.
    self.finish_oauth_flow(auth_entity, state)


class StartHandler(util.Handler):
  """Custom handler that sets OAuth scopes based on the requested
  feature(s)
  """
  def post(self):
    features = self.request.get('feature')
    features = features.split(',') if features else []
    starter = util.oauth_starter(oauth_facebook.StartHandler).to(
      '/facebook/oauth_handler', scopes=sorted(set(
        (LISTEN_SCOPES if 'listen' in features else []) +
        (PUBLISH_SCOPES if 'publish' in features else []))))
    starter(self.request, self.response).post()


application = webapp2.WSGIApplication([
    ('/facebook/start', StartHandler),
    ('/facebook/oauth_handler', OAuthCallback),
    ('/facebook/add', AddFacebookPage),
    ('/facebook/delete/finish', oauth_facebook.CallbackHandler.to('/delete/finish')),
    ('/facebook/publish/start', oauth_facebook.StartHandler.to(
      '/publish/facebook/finish')),
    ], debug=appengine_config.DEBUG)
