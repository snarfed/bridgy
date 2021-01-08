"""Instagram code and datastore model classes.
"""
from datetime import timedelta
import logging
from operator import itemgetter

from google.cloud import ndb
from granary import instagram as gr_instagram
from granary import microformats2
from granary import source as gr_source
from oauth_dropins.webutil.util import json_dumps, json_loads
import webapp2

from oauth_dropins import indieauth
from oauth_dropins import instagram as od_instagram

from models import Activity, Domain, Source, MAX_AUTHOR_URLS
import util

JSON_CONTENT_TYPE = 'application/json'


def merge_by_id(existing, updates):
  """Merges two lists of AS1 objects by id.

  Overwrites the objects in the existing list with objects in the updates list
  with the same id. Requires all objects to have ids.

  Args:
    existing: sequence of AS1 dicts
    updates: sequence of AS1 dicts

  Returns: merged list of AS1 dicts
  """
  objs = {o['id']: o for o in existing}
  objs.update({o['id']: o for o in updates})
  return sorted(objs.values(), key=itemgetter('id'))


class Instagram(Source):

  """An Instagram account.

  The key name is the username. Instagram usernames may have ASCII letters (case
  insensitive), numbers, periods, and underscores:
  https://stackoverflow.com/questions/15470180
  """
  GR_CLASS = gr_instagram.Instagram
  SHORT_NAME = 'instagram'
  CAN_LISTEN = True
  CAN_PUBLISH = False
  AUTO_POLL = False
  SLOW_POLL = FAST_POLL = timedelta(0)
  OAUTH_START_HANDLER = None
  URL_CANONICALIZER = util.UrlCanonicalizer(
    domain=GR_CLASS.DOMAIN,
    subdomain='www',
    approve=r'https://www.instagram.com/p/[^/?]+/$',
    trailing_slash=True,
    headers=util.REQUEST_HEADERS)
    # no reject regexp; non-private Instagram post URLs just 404

  # blank granary Instagram object, shared across all instances
  gr_source = gr_instagram.Instagram()

  @staticmethod
  def new(handler, auth_entity=None, actor=None, **kwargs):
    """Creates and returns an :class:`Instagram` for the logged in user.

    Args:
      handler: the current :class:`webapp2.RequestHandler`
    """
    assert not auth_entity
    assert actor

    username = actor['username']
    if not kwargs.get('features'):
      kwargs['features'] = ['listen']

    ig = Instagram(id=username,
                   name=actor.get('displayName'),
                   picture=actor.get('image', {}).get('url'),
                   url=gr_instagram.Instagram.user_url(username),
                   **kwargs)
    ig.domain_urls, ig.domains = ig._urls_and_domains(None, None, actor=actor)
    return ig

  def silo_url(self):
    """Returns the Instagram account URL, e.g. https://instagram.com/foo."""
    return self.url

  def label_name(self):
    """Returns the username."""
    return self.key_id()

  @classmethod
  def button_html(cls, feature, **kwargs):
    return od_instagram.StartHandler.button_html(
      '/about#browser-extension',
      form_method='get',
      image_prefix='/oauth_dropins/static/')

  def get_activities_response(self, *args, **kwargs):
    """Uses Activity entities stored in the datastore."""
    activities = []

    activity_id = kwargs.get('activity_id')
    if activity_id:
      activity = Activity.get_by_id(self.gr_source.tag_uri(activity_id))
      if activity:
        activities = [activity]
    else:
      activities = Activity.query(Activity.source == self.key)\
                           .order(-Activity.updated).fetch(50)

    return self.gr_source.make_activities_base_response(
      [json_loads(a.activity_json) for a in activities])

  def get_comment(self, comment_id,  activity=None, **kwargs):
    """Uses the activity passed in the activity kwarg."""
    if activity:
      for reply in activity.get('object', {}).get('replies', {}).get('items', []):
        parsed = util.parse_tag_uri(reply.get('id', ''))
        if parsed and parsed[1] == comment_id:
          return reply

  def get_like(self, activity_user_id, activity_id, like_user_id, activity=None,
               **kwargs):
    """Uses the activity passed in the activity kwarg."""
    if activity:
      for tag in activity.get('object', {}).get('tags', []):
        if tag.get('verb') == 'like':
          parsed = util.parse_tag_uri(tag.get('author', {}).get('id', ''))
          if parsed and parsed[1] == like_user_id:
            return tag


class BrowserHandler(util.Handler):
  """Base class for requests from the browser extension."""
  ig = gr_instagram.Instagram()

  def options(self):
    self.response.headers['Access-Control-Allow-Origin'] = '*'
    self.response.headers['Access-Control-Allow-Methods'] = '*'

  def output(self, obj):
    self.response.headers['Content-Type'] = JSON_CONTENT_TYPE
    self.response.write(json_dumps(obj, indent=2))

  def parse_activities(self):
    """Parses Instagram HTML (in the POST body) into logged in user and posts.

    Returns:
      (list of dicts, dict): AS1 activities and logged in actor
    """
    # parse the Instagram profile HTML
    activities, actor = self.ig.html_to_activities(self.request.text)
    if not actor or not actor.get('username'):
      self.abort(400, "Couldn't determine logged in Instagram user")

    # check that the instagram account is public
    if not gr_source.Source.is_public(actor):
      self.abort(400, 'Your Instagram account is private. Bridgy only supports public accounts.')

    return activities, actor

  def check_token(self, actor):
    """If the request's token is not stored for the actor's domain, returns 400.

    Args:
      actor: dict, AS1 actor for logged in Instagram user
    """
    token = util.get_required_param(self, 'token')

    domains = set(util.domain_from_link(u) for u in microformats2.object_urls(actor))
    domains.discard(gr_instagram.Instagram.DOMAIN)
    for domain in ndb.get_multi(ndb.Key(Domain, d) for d in domains):
      if domain and token in domain.tokens:
        return

    self.abort(400, f'Token {token} is not authorized for any of: {domains}')


class HomepageHandler(BrowserHandler):
  """Parses an Instagram home page and returns the logged in user's username.

  Request body is https://www.instagram.com/ HTML for a logged in user.
  """
  def post(self):
    _, actor = self.parse_activities()
    logging.info(f"Returning {actor['username']}")
    self.output(actor['username'])


class ProfileHandler(BrowserHandler):
  """Parses an Instagram profile page and returns the posts' URLs.

  Request body is HTML from an IG profile, eg https://www.instagram.com/name/ ,
  for a logged in user.

  Response body is the JSON list of translated ActivityStreams activities.
  """
  def post(self):
    activities, actor = self.parse_activities()
    self.check_token(actor)

    # create/update the Bridgy account
    Instagram.create_new(self, actor=actor)

    ids = ' '.join(a['id'] for a in activities)
    logging.info(f"Returning activities for {actor['username']}: {ids}")
    self.output(activities)


class PostHandler(BrowserHandler):
  """Parses an Instagram post and creates new Responses as needed.

  Request body is HTML from an IG photo/video permalink, eg
  https://www.instagram.com/p/ABC123/ , for a logged in user.

  Response body is the translated ActivityStreams activity JSON.
  """
  @ndb.transactional()
  def post(self):
    activities, actor = self.parse_activities()
    self.check_token(actor)

    if len(activities) != 1:
      self.abort(400, f'Expected 1 Instagram post, got {len(activities)}')
    activity_data = activities[0]
    id = activity_data.get('id')
    if not id:
      self.abort(400, 'Instagram post missing id')

    username = activity_data['object']['author']['username']
    source = Instagram.get_by_id(username)
    if not source:
      self.abort(404, f'No account found for Instagram user {username}')

    activity = Activity.get_by_id(id)
    if activity:
      # we already have this activity! merge in any new comments.
      existing = json_loads(activity.activity_json)
      comments = merge_by_id(
        existing['object'].get('replies', {}).get('items', []),
        activity_data['object'].get('replies', {}).get('items', []))
      activity_data['object']['replies'] = {
        'items': comments,
        'totalItems': len(comments),
      }

    # store the new activity
    Activity(id=id, source=source.key, activity_json=json_dumps(activity_data)).put()
    logging.info(f"Stored activity {id}")
    self.output(activity_data)


class LikesHandler(BrowserHandler):
  """Parses a list of Instagram likes and adds them to an existing Activity.

  Requires the request parameter `id` with the IG post's id (not shortcode!).

  Request body is a JSON list of IG likes for a post that's already been created
  via the /instagram/browser/post endpoint.

  Response body is the translated ActivityStreams JSON for the likes.
  """
  def post(self):
    id = util.get_required_param(self, 'id')

    # validate request
    parsed_id = util.parse_tag_uri(id)
    if not parsed_id:
      self.abort(400, f'Expected id to be tag URI; got {id}')

    activity = Activity.get_by_id(id)
    if not activity:
      self.abort(404, f'No Instagram post found for id {id}')

    activity_data = json_loads(activity.activity_json)
    obj = activity_data['object']
    actor = obj.get('author') or activity.get('actor')
    self.check_token(actor=actor)

    # convert new likes to AS
    container = {
      'id': parsed_id[1],
      'edge_media_preview_like': {
        # corresponds to same code in gr_instagram.Instagram.html_to_activities()
        'edges': self.request.json.get('data', {}).get('shortcode_media', {})\
                                  .get('edge_liked_by', {}).get('edges', [])
      },
    }
    new_likes = self.ig._json_media_node_to_activity(container)['object']['tags']

    # merge them into existing activity
    obj['tags'] = merge_by_id(obj.get('tags', []), new_likes)
    activity.activity_json = json_dumps(activity_data)
    activity.put()

    like_ids = ' '.join(l['id'] for l in new_likes)
    logging.info(f"Stored likes for activity {id}: {like_ids}")
    self.output(new_likes)


class PollHandler(BrowserHandler):
  """Triggers a poll for an Instagram account.

  Requires the `username` parameter.
  """
  def post(self):
    username = util.get_required_param(self, 'username')
    source = Instagram.get_by_id(username)
    if not source:
      self.abort(404, f'No account found for Instagram user {username}')

    util.add_poll_task(source)

    self.output('OK')


class TokenDomainsHandler(BrowserHandler):
  """Returns the domains that a token is registered for."""
  def post(self):
    token = util.get_required_param(self, 'token')

    domains = [d.key.id() for d in Domain.query(Domain.tokens == token)]
    if not domains:
      self.abort(404, f'No registered domains for token {token}')

    self.output(domains)


ROUTES = [
  ('/instagram/browser/homepage', HomepageHandler),
  ('/instagram/browser/profile', ProfileHandler),
  ('/instagram/browser/post', PostHandler),
  ('/instagram/browser/likes', LikesHandler),
  ('/instagram/browser/poll', PollHandler),
  ('/instagram/browser/token-domains', TokenDomainsHandler),
]
