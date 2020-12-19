"""Instagram code and datastore model classes.
"""
from datetime import timedelta
import logging
from operator import itemgetter

from granary import instagram as gr_instagram
from granary import microformats2
from granary import source as gr_source
from oauth_dropins.webutil.util import json_dumps, json_loads
import webapp2

from oauth_dropins import indieauth

from models import Activity, Source
import util

AS1_JSON_CONTENT_TYPE = 'application/stream+json'


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
    return super(cls, cls).button_html(feature, form_method='get', **kwargs)

  def get_activities_response(self, *args, **kwargs):
    """Set user_id because scraping requires it."""
    kwargs.setdefault('group_id', gr_source.SELF)
    kwargs.setdefault('user_id', self.key_id())
    return self.gr_source.get_activities_response(*args, **kwargs)


class HomepageHandler(util.Handler):
  """Parses an Instagram home page and returns the logged in user's username.

  Request body is https://www.instagram.com/ HTML for a logged in user.
  """
  def post(self):
    ig = gr_instagram.Instagram()
    _, actor = ig.html_to_activities(self.request.text)
    if not actor or not actor.get('username'):
      self.abort(400, "Couldn't determine logged in Instagram user")

    self.response.headers['Content-Type'] = 'text/plain'
    self.response.write(actor['username'])


class ProfileHandler(util.Handler):
  """Parses an Instagram profile page and returns the posts' URLs.

  Request body is HTML from an IG profile, eg https://www.instagram.com/name/ ,
  for a logged in user.

  Response body is the JSON list of translated ActivityStreams activities.
  """
  def post(self):
    # parse the Instagram profile HTML
    ig = gr_instagram.Instagram()
    activities, actor = ig.html_to_activities(self.request.text)
    if not actor or not actor.get('username'):
      self.abort(400, "Couldn't determine logged in Instagram user")

    # check that the instagram account is public
    if not gr_source.Source.is_public(actor):
      self.abort(400, 'Your Instagram account is private. Bridgy only supports public accounts.')

    # create/update the Bridgy account
    Instagram.create_new(self, actor=actor)

    self.response.headers['Content-Type'] = AS1_JSON_CONTENT_TYPE
    self.response.write(json_dumps(activities, indent=2))


class PostHandler(util.Handler):
  """Parses an Instagram post and creates new Responses as needed.

  Request body is HTML from an IG photo/video permalink, eg
  https://www.instagram.com/p/ABC123/ , for a logged in user.

  Response body is the translated ActivityStreams activity JSON.

  TODO: merge new comments into existing activities
  """
  def post(self):
    ig = gr_instagram.Instagram()
    activities, _ = ig.html_to_activities(self.request.text)

    if len(activities) != 1:
      self.abort(400, f'Expected 1 Instagram post, got {len(activities)}')
    activity = activities[0]
    if not activity['id']:
      self.abort(400, 'Instagram post missing id')

    obj = activity['object']
    username = obj['author']['username']
    source = Instagram.get_by_id(username)
    if not source:
      self.abort(404, f'No account found for Instagram user {username}')

    activity_json = json_dumps(activity, indent=2)
    Activity.get_or_insert(activity['id'], source=source.key,
                           activity_json=activity_json)

    self.response.headers['Content-Type'] = AS1_JSON_CONTENT_TYPE
    self.response.write(activity_json)


class LikesHandler(util.Handler):
  """Parses a list of Instagram likes and adds them to an existing Activity.

  Requires the request parameter `shortcode` with the IG post's shortcode.

  Request body is a JSON list of IG likes for a post that's already been created
  via the /instagram/browser/post endpoint.

  Response body is the translated ActivityStreams JSON for the likes.
  """
  def post(self):
    id = util.get_required_param(self, 'id')
    parsed = util.parse_tag_uri(id)
    if not parsed:
      self.abort(400, f'Expected id to be tag URI; got {id}')

    activity = Activity.get_by_id(id)
    if not activity:
      self.abort(404, f'No Instagram post found for id {id}')

    _, id = parsed

    if not self.request.json:
      self.response.headers['Content-Type'] = AS1_JSON_CONTENT_TYPE
      self.response.write(json_dumps([]))
      return

    # convert new likes to AS
    container = {
      'id': id,
      'edge_media_preview_like': {
        'edges': [{'node': like} for like in self.request.json],
      },
    }
    ig = gr_instagram.Instagram()
    new_likes = ig._json_media_node_to_activity(container)['object']['tags']

    # merge them into existing activity
    activity_data = json_loads(activity.activity_json)
    existing = {id: tag for tag in activity_data['object'].get('tags', [])}
    existing.update({like['id']: like for like in new_likes})

    activity_data['object']['tags'] = list(existing.values())
    activity.activity_json = json_dumps(activity_data)
    activity.put()

    self.response.headers['Content-Type'] = AS1_JSON_CONTENT_TYPE
    self.response.write(json_dumps(new_likes, indent=2))


class PollHandler(util.Handler):
  """Triggers a poll for an Instagram account.

  Requires the `username` parameter.
  """
  def post(self):
    username = util.get_required_param(self, 'username')
    source = Instagram.get_by_id(username)
    if not source:
      self.abort(404, f'No account found for Instagram user {username}')

    util.add_poll_task(source)

ROUTES = [
  ('/instagram/browser/homepage', HomepageHandler),
  ('/instagram/browser/profile', ProfileHandler),
  ('/instagram/browser/post', PostHandler),
  ('/instagram/browser/likes', LikesHandler),
  ('/instagram/browser/poll', PollHandler),
]
