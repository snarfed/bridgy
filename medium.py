"""Medium hosted blog implementation.

Only supports outbound webmentions right now, not inbound, since Medium's API
doesn't support creating responses or recommendations yet.
https://github.com/Medium/medium-api-docs/issues/71
https://github.com/Medium/medium-api-docs/issues/72

API docs:
https://github.com/Medium/medium-api-docs#contents
https://medium.com/developers/welcome-to-the-medium-api-3418f956552
"""

import collections
import json
import logging

import appengine_config

from oauth_dropins import medium as oauth_medium
import models
import superfeedr
import util
import webapp2


class Medium(models.Source):
  """A Medium publication or user blog.

  The key name is the username (with @ prefix) or publication name.
  """
  GR_CLASS = collections.namedtuple('FakeGrClass', ('NAME',))(NAME='Medium')
  SHORT_NAME = 'medium'

  def feed_url(self):
    # https://help.medium.com/hc/en-us/articles/214874118-RSS-Feeds-of-publications-and-profiles
    return 'https://medium.com/feed/' + self.key.id()

  def silo_url(self):
    return self.domain_urls[0]

  @staticmethod
  def new(handler, auth_entity=None, **kwargs):
    """Creates and returns a Medium for the logged in user.

    Args:
      handler: the current RequestHandler
      auth_entity: oauth_dropins.medium.MediumAuth
    """
    # TODO
    # publications_json = Medium.get_publications(handler, auth_entity)
    # urls = util.dedupe_urls(util.trim_nulls(
    #   [site_info.get('URL'), auth_entity.blog_url]))
    # domains = [util.domain_from_link(u) for u in urls]

    data = json.loads(auth_entity.user_json)['data']
    username = data['username']
    if not username.startswith('@'):
      username = '@' + username

    return Medium(id=username,
                  auth_entity=auth_entity.key,
                  name=auth_entity.user_display_name(),
                  picture=data['imageUrl'],
                  superfeedr_secret=util.generate_secret(),
                  url=data['url'],
                  **kwargs)

  def verified(self):
    return True

  # TODO: something better?
  def has_bridgy_webmention_endpoint(self):
    return True

  def _urls_and_domains(self, auth_entity, user_url):
    url = json.loads(auth_entity.user_json)['data']['url']
    return ([url], [util.domain_from_link(url)])

class AddMedium(oauth_medium.CallbackHandler, util.Handler):
  def finish(self, auth_entity, state=None):
    self.maybe_add_or_delete_source(Medium, auth_entity, state)


class SuperfeedrNotifyHandler(superfeedr.NotifyHandler):
  SOURCE_CLS = Medium


application = webapp2.WSGIApplication([
    # https://github.com/Medium/medium-api-docs#user-content-21-browser-based-authentication
    ('/medium/start', util.oauth_starter(oauth_medium.StartHandler).to(
      '/medium/add')),
    ('/medium/add', AddMedium),
    ('/medium/delete/finish', oauth_medium.CallbackHandler.to('/delete/finish')),
    ('/medium/notify/(.+)', SuperfeedrNotifyHandler),
    ], debug=appengine_config.DEBUG)
