"""Instagram code and datastore model classes.
"""
import logging

from granary import instagram as gr_instagram
from granary import microformats2
from granary import source as gr_source
from oauth_dropins.webutil.util import json_dumps, json_loads
import webapp2

from oauth_dropins import indieauth
# from oauth_dropins import instagram as oauth_instagram

from models import Activity, Source
import util


class Instagram(Source):
  """An Instagram account.

  The key name is the username. Instagram usernames may have ASCII letters (case
  insensitive), numbers, periods, and underscores:
  https://stackoverflow.com/questions/15470180
  """
  GR_CLASS = gr_instagram.Instagram
  SHORT_NAME = 'instagram'
  CAN_LISTEN = False
  CAN_PUBLISH = False
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

    self.response.write(actor['username'])


class ProfileHandler(util.Handler):
  """Parses an Instagram profile page and returns the posts' URLs.

  Request body is HTML from an IG profile, eg https://www.instagram.com/name/ ,
  for a logged in user.
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
    self.response.write(json_dumps([a['object']['url'] for a in activities]))


class PostHandler(util.Handler):
  """Parses an Instagram post and creates new Responses as needed.

  Request body is HTML from an IG photo/video permalink, eg
  https://www.instagram.com/p/ABC123/ , for a logged in user.

  Response body is the translated ActivityStreams activity JSON.
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
      self.abort(400, f'No account found for Instagram user {username}')

    activity_json = json_dumps(activity, indent=2)
    Activity.get_or_insert(activity['id'], source=source.key,
                           activity_json=activity_json)
    self.response.write(activity_json)


ROUTES = [
  ('/instagram/browser/homepage', HomepageHandler),
  ('/instagram/browser/profile', ProfileHandler),
  ('/instagram/browser/post', PostHandler),
]
