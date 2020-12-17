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

from models import Source
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

  @staticmethod
  def new(handler, auth_entity=None, actor=None, **kwargs):
    """Creates and returns an :class:`Instagram` for the logged in user.

    Args:
      handler: the current :class:`webapp2.RequestHandler`
    """
    assert auth_entity is None

    username = actor['username']
    if not kwargs.get('features'):
      kwargs['features'] = ['listen']
    return Instagram(id=username,
                     name=actor.get('displayName'),
                     picture=actor.get('image', {}).get('url'),
                     url=gr_instagram.Instagram.user_url(username),
                     **kwargs)

  def silo_url(self):
    """Returns the Instagram account URL, e.g. https://instagram.com/foo."""
    return self.url

  # def user_tag_id(self):
  #   """Returns the tag URI for this source, e.g. 'tag:instagram.com:123456'."""
  #   user = json_loads(self.auth_entity.get().user_json)
  #   return (user.get('actor', {}).get('id') or
  #           self.gr_source.tag_uri(user.get('id') or self.key_id()))

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
  """Parses an Instagram home page and returns the logged in user's username."""
  def post(self):
    ig = gr_instagram.Instagram()
    _, actor = ig.html_to_activities(self.request.text)
    if not actor or not actor.get('username'):
      self.abort(400, "Couldn't determine logged in Instagram user")

    self.response.write(actor['username'])


    #   # check that instagram profile links to web site
    #   actor = gr_instagram.Instagram(
    #     scrape=True, cookie=oauth_instagram.INSTAGRAM_SESSIONID_COOKIE
    #   ).get_actor(username, ignore_rate_limit=True)

    #   canonicalize = util.UrlCanonicalizer(redirects=False)
    #   website = canonicalize(auth_entity.key.id())
    #   urls = [canonicalize(u) for u in microformats2.object_urls(actor)]
    #   logging.info('Looking for %s in %s', website, urls)
    #   if website not in urls:
    #     self.messages.add("Please add %s to your Instagram profile's website or bio field and try again." % website)
    #     return self.redirect('/')

    #   # check that the instagram account is public
    #   if not gr_source.Source.is_public(actor):
    #     self.messages.add('Your Instagram account is private. Bridgy only supports public accounts.')
    #     return self.redirect('/')

    # self.maybe_add_or_delete_source(Instagram, auth_entity, state, actor=actor)


ROUTES = [
  ('/instagram/browser/homepage', HomepageHandler),
]
