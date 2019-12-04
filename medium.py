"""Medium hosted blog implementation.

Only supports outbound webmentions right now, not inbound, since Medium's API
doesn't support creating responses or recommendations yet.
https://github.com/Medium/medium-api-docs/issues/71
https://github.com/Medium/medium-api-docs/issues/72

API docs:
https://github.com/Medium/medium-api-docs#contents
https://medium.com/developers/welcome-to-the-medium-api-3418f956552
"""
from __future__ import unicode_literals

from builtins import str
import collections
import logging

import appengine_config

from google.cloud import ndb
from oauth_dropins import medium as oauth_medium
from oauth_dropins.webutil.handlers import JINJA_ENV
from oauth_dropins.webutil.util import json_dumps, json_loads
import webapp2

import models
import superfeedr
import util


class Medium(models.Source):
  """A Medium publication or user blog.

  The key name is the username (with @ prefix) or publication name.
  """
  GR_CLASS = collections.namedtuple('FakeGrClass', ('NAME',))(NAME='Medium')
  OAUTH_START_HANDLER = oauth_medium.StartHandler
  SHORT_NAME = 'medium'


  def is_publication(self):
    return not self.key.id().startswith('@')

  def feed_url(self):
    # https://help.medium.com/hc/en-us/articles/214874118-RSS-Feeds-of-publications-and-profiles
    return self.url.replace('medium.com/', 'medium.com/feed/')

  def silo_url(self):
    return self.url

  @staticmethod
  def new(handler, auth_entity=None, id=None, **kwargs):
    """Creates and returns a Medium for the logged in user.

    Args:
      handler: the current :class:`webapp2.RequestHandler`
      auth_entity: :class:`oauth_dropins.medium.MediumAuth`
      id: string, either username (starting with @) or publication id
    """
    assert id
    medium = Medium(id=id,
                    auth_entity=auth_entity.key,
                    superfeedr_secret=util.generate_secret(),
                    **kwargs)

    data = medium._data(auth_entity)
    medium.name = data.get('name') or data.get('username')
    medium.picture = data.get('imageUrl')
    medium.url = data.get('url')
    return medium

  def verified(self):
    return False

  def verify(self, force=False):
    """No incoming webmention support yet."""
    pass

  def has_bridgy_webmention_endpoint(self):
    return True

  def _data(self, auth_entity):
    """Returns the Medium API object for this user or publication.

    https://github.com/Medium/medium-api-docs/#user-content-getting-the-authenticated-users-details

    Example user::
        {
          'imageUrl': 'https://cdn-images-1.medium.com/fit/c/200/200/0*4dsrv3pwIJfFraSz.jpeg',
          'url': 'https://medium.com/@snarfed',
          'name': 'Ryan Barrett',
          'username': 'snarfed',
          'id': '113863a5ca2ab60671e8c9fe089e59c07acbf8137c51523605dc55528516c0d7e'
        }

    Example publication::
        {
          'id': 'b45573563f5a',
          'name': 'Developers',
          'description': "Medium's Developer resources",
          'url': 'https://medium.com/developers',
          'imageUrl': 'https://cdn-images-1.medium.com/fit/c/200/200/1*ccokMT4VXmDDO1EoQQHkzg@2x.png'
        }
    """
    id = self.key.id().lstrip('@')

    user = json_loads(auth_entity.user_json).get('data')
    if user.get('username').lstrip('@') == id:
      return user

    for pub in json_loads(auth_entity.publications_json).get('data', []):
      if pub.get('id') == id:
        return pub

  def _urls_and_domains(self, auth_entity, user_url):
    if self.url:
      return [self.url], [util.domain_from_link(self.url)]

    return [], []


class AddMedium(util.Handler):
  def post(self):
    auth_entity = ndb.Key(
      urlsafe=util.get_required_param(self, 'auth_entity_key')).get()
    state = util.get_required_param(self, 'state')
    id = util.get_required_param(self, 'blog')
    self.maybe_add_or_delete_source(Medium, auth_entity, state, id=id)


class ChooseBlog(oauth_medium.CallbackHandler, util.Handler):
  def finish(self, auth_entity, state=None):
    if not auth_entity:
      self.maybe_add_or_delete_source(Medium, auth_entity, state)
      return

    user = json_loads(auth_entity.user_json)['data']
    username = user['username']
    if not username.startswith('@'):
      username = '@' + username

    # fetch publications this user contributes or subscribes to.
    # (sadly medium's API doesn't tell us the difference unless we fetch each
    # pub's metadata separately.)
    # https://github.com/Medium/medium-api-docs/#user-content-listing-the-users-publications
    auth_entity.publications_json = auth_entity.get(
      oauth_medium.API_BASE + 'users/%s/publications' % user['id']).text
    auth_entity.put()
    pubs = json_loads(auth_entity.publications_json).get('data')
    if not pubs:
      self.maybe_add_or_delete_source(Medium, auth_entity, state,
                                      id=username)
      return

    # add user profile to start of pubs list
    user['id'] = username
    pubs.insert(0, user)

    vars = {
      'action': '/medium/add',
      'state': state,
      'auth_entity_key': auth_entity.key.urlsafe(),
      'blogs': [{
        'id': p['id'],
        'title': p.get('name', ''),
        'url': p.get('url', ''),
        'pretty_url': util.pretty_link(str(p.get('url', ''))),
        'image': p.get('imageUrl', ''),
      } for p in pubs if p.get('id')],
    }
    logging.info('Rendering choose_blog.html with %s', vars)
    self.response.headers['Content-Type'] = 'text/html'
    self.response.out.write(JINJA_ENV.get_template('choose_blog.html').render(**vars))


class SuperfeedrNotifyHandler(superfeedr.NotifyHandler):
  SOURCE_CLS = Medium


application = webapp2.WSGIApplication([
    # https://github.com/Medium/medium-api-docs#user-content-21-browser-based-authentication
    ('/medium/start', util.oauth_starter(oauth_medium.StartHandler).to(
      '/medium/choose_blog', scopes=('basicProfile', 'listPublications'))),
    ('/medium/add', AddMedium),
    ('/medium/choose_blog', ChooseBlog),
    ('/medium/delete/finish', oauth_medium.CallbackHandler.to('/delete/finish')),
    ('/medium/notify/(.+)', SuperfeedrNotifyHandler),
    ], debug=appengine_config.DEBUG)
