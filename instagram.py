"""Instagram API code and datastore model classes.

Example post ID and links
  id: 595990791004231349 or 595990791004231349_247678460
    (suffix is user id)
  Permalink: http://instagram.com/p/hFYnd7Nha1/
  API URL: https://api.instagram.com/v1/media/595990791004231349
  Local handler path: /post/instagram/212038/595990791004231349

Example comment ID and links
  id: 595996024371549506
  No direct API URL or permalink, as far as I can tell. :/
  API URL for all comments on that picture:
    https://api.instagram.com/v1/media/595990791004231349_247678460/comments
  Local handler path:
    /comment/instagram/212038/595990791004231349_247678460/595996024371549506
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import json
import logging
import urlparse

import appengine_config
from granary import instagram as gr_instagram
from granary import microformats2
from granary import source as gr_source
from oauth_dropins import indieauth
from oauth_dropins import instagram as oauth_instagram
from oauth_dropins.webutil.handlers import TemplateHandler
import webapp2

import models
import util


class Instagram(models.Source):
  """An Instagram account.

  The key name is the username.
  """

  GR_CLASS = gr_instagram.Instagram
  SHORT_NAME = 'instagram'

  URL_CANONICALIZER = util.UrlCanonicalizer(
    domain=GR_CLASS.DOMAIN,
    subdomain='www',
    approve=r'https://www.instagram.com/p/[^/?]+/',
    trailing_slash=True,
    headers=util.USER_AGENT_HEADER)
    # no reject regexp; non-private Instagram post URLs just 404

  @staticmethod
  def new(handler, auth_entity=None, actor=None, **kwargs):
    """Creates and returns a InstagramPage for the logged in user.

    Args:
      handler: the current RequestHandler
      auth_entity: oauth_dropins.instagram.InstagramAuth
    """
    user = json.loads(auth_entity.user_json)
    user['actor'] = actor
    auth_entity.user_json = json.dumps(user)
    auth_entity.put()

    username = actor['username']
    if not kwargs.get('features'):
      kwargs['features'] = ['listen']
    urls = util.dedupe_urls(util.trim_nulls(actor.get('urls', []) + [actor.get('url')]))
    return Instagram(id=username,
                     auth_entity=auth_entity.key,
                     name=actor.get('displayName'),
                     picture=actor.get('image', {}).get('url'),
                     url=gr_instagram.Instagram.user_url(username),
                     domain_urls=urls,
                     domains=[util.domain_from_link(url) for url in urls],
                     **kwargs)

  def silo_url(self):
    """Returns the Instagram account URL, e.g. https://instagram.com/foo."""
    return self.url

  def user_tag_id(self):
    """Returns the tag URI for this source, e.g. 'tag:instagram.com:123456'."""
    user = json.loads(self.auth_entity.get().user_json)
    return self.gr_source.tag_uri(user.get('id') or self.key.id())

  def label_name(self):
    """Returns the username."""
    return self.key.id()

  def get_activities_response(self, *args, **kwargs):
    """Set user_id because scraping requires it."""
    kwargs.setdefault('group_id', gr_source.SELF)
    kwargs.setdefault('user_id', self.key.id())
    return self.gr_source.get_activities_response(*args, **kwargs)


class StartHandler(TemplateHandler):
  """Serves the "Enter your username" form page."""
  def template_file(self):
    return 'templates/indieauth.html'


class CallbackHandler(indieauth.CallbackHandler, util.Handler):
  def finish(self, auth_entity, state=None):
    if auth_entity:
      user_json = json.loads(auth_entity.user_json)

      # find instagram profile URL
      urls = user_json.get('rel-me', [])
      logging.info('rel-mes: %s', urls)
      for url in util.trim_nulls(urls):
        if util.domain_from_link(url) == gr_instagram.Instagram.DOMAIN:
          username = urlparse.urlparse(url).path.strip('/')
          break
      else:
        self.messages.add(
          'No Instagram profile found. Please <a href="https://indieauth.com/setup">'
          'add an Instagram rel-me link</a>, then try again.')
        return self.redirect_home_or_user_page(state)

      # check that instagram profile links to web site
      actor = gr_instagram.Instagram(scrape=True).get_actor(username)

      canonicalize = util.UrlCanonicalizer(redirects=False)
      website = canonicalize(auth_entity.key.id())
      urls = [canonicalize(u) for u in util.trim_nulls(
                actor.get('urls', []) + [actor.get('url')])]
      logging.info('Looking for %s in %s', website, urls)
      if website not in urls:
        self.messages.add("Please add %s to your Instagram profile's website or "
                          'bio field and try again.' % website)
        return self.redirect_home_or_user_page(state)

      # check that the instagram account is public
      if not gr_source.Source.is_public(actor):
        self.messages.add('Your Instagram account is private. '
                          'Bridgy only supports public accounts.')
        return self.redirect_home_or_user_page(state)

    source = self.maybe_add_or_delete_source(Instagram, auth_entity, state,
                                             actor=actor)


application = webapp2.WSGIApplication([
    ('/instagram/start', StartHandler),
    ('/instagram/indieauth', indieauth.StartHandler.to('/instagram/callback')),
    ('/instagram/callback', CallbackHandler),
], debug=appengine_config.DEBUG)
