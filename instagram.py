"""Instagram browser extension source class and request handlers.
"""
from granary import instagram as gr_instagram
from oauth_dropins import instagram as oauth_instagram

import browser
import util


class Instagram(browser.BrowserSource):
  """An Instagram account.

  The key name is the username. Instagram usernames may have ASCII letters (case
  insensitive), numbers, periods, and underscores:
  https://stackoverflow.com/questions/15470180
  """
  GR_CLASS = gr_instagram.Instagram
  SHORT_NAME = 'instagram'
  OAUTH_START_HANDLER = oauth_instagram.StartHandler
  URL_CANONICALIZER = util.UrlCanonicalizer(
    domain=GR_CLASS.DOMAIN,
    subdomain='www',
    approve=r'https://www.instagram.com/p/[^/?]+/$',
    trailing_slash=True,
    headers=util.REQUEST_HEADERS)
    # no reject regexp; non-private Instagram post URLs just 404

  # blank granary Instagram object, shared across all instances
  gr_source = gr_instagram.Instagram()

  @classmethod
  def key_id_from_actor(cls, actor):
    """Returns the actor's username field to be used as this entity's key id."""
    return actor['username']

  def silo_url(self):
    """Returns the Instagram account URL, e.g. https://instagram.com/foo."""
    return self.gr_source.user_url(self.key.id())

  def label_name(self):
    """Returns the username."""
    return self.key_id()


ROUTES = browser.routes(Instagram)
