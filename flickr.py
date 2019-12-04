"""Flickr source and data model storage class."""
from __future__ import unicode_literals

import appengine_config
import datetime
import logging
import models
import util
import webapp2

from google.cloud import ndb
from granary import flickr as gr_flickr
from granary.source import SELF
from oauth_dropins import flickr as oauth_flickr
from oauth_dropins.webutil.util import json_dumps, json_loads


class Flickr(models.Source):
  """A Flickr account.

  The key name is the nsid.
  """
  # Fetching comments and likes is extremely request-intensive, so let's dial
  # back the frequency for now.
  FAST_POLL = datetime.timedelta(minutes=60)
  GR_CLASS = gr_flickr.Flickr
  OAUTH_START_HANDLER = oauth_flickr.StartHandler
  SHORT_NAME = 'flickr'
  TRANSIENT_ERROR_HTTP_CODES = ('400',)
  CAN_PUBLISH = True
  URL_CANONICALIZER = util.UrlCanonicalizer(
    domain=GR_CLASS.DOMAIN,
    approve=r'https://www\.flickr\.com/(photos|people)/[^/?]+/([^/?]+/)?$',
    reject=r'https://login\.yahoo\.com/.*',
    subdomain='www',
    trailing_slash=True,
    headers=util.REQUEST_HEADERS)

  # unique name optionally used in URLs instead of nsid (e.g.,
  # flickr.com/photos/username)
  username = ndb.StringProperty()

  @staticmethod
  def new(handler, auth_entity=None, **kwargs):
    """Creates and returns a :class:`Flickr` for the logged in user.

    Args:
      handler: the current :class:`webapp2.RequestHandler`
      auth_entity: :class:`oauth_dropins.flickr.FlickrAuth`
    """
    person = json_loads(auth_entity.user_json).get('person', {})
    return Flickr(
      id=person.get('nsid'),
      auth_entity=auth_entity.key,
      name=person.get('realname', {}).get('_content'),
      # path_alias, if it exists, is the actual thing that shows up in the url.
      # I think this is an artifact of the conversion to Yahoo.
      username=(person.get('path_alias')
                or person.get('username', {}).get('_content')),
      picture='https://farm{}.staticflickr.com/{}/buddyicons/{}.jpg' .format(
        person.get('iconfarm'), person.get('iconserver'),
        person.get('nsid')),
      url=person.get('profileurl', {}).get('_content'),
      **kwargs)

  def silo_url(self):
    """Returns the Flickr account URL, e.g. https://www.flickr.com/people/foo/."""
    return self.url

  def user_tag_id(self):
    """Returns the tag URI for this source, e.g. 'tag:instagram.com:123456'."""
    return self.gr_source.tag_uri(self.username)

  def get_activities_response(self, *args, **kwargs):
    """Discard min_id because we still want new comments/likes on old photos."""
    kwargs.setdefault('group_id', SELF)
    if 'min_id' in kwargs:
      del kwargs['min_id']
    return self.gr_source.get_activities_response(*args, **kwargs)

  def canonicalize_url(self, url, activity=None, **kwargs):
    if not url.endswith('/'):
      url = url + '/'
    if self.username:
      url = url.replace('flickr.com/photos/%s/' % self.username,
                        'flickr.com/photos/%s/' % self.key.id())
      url = url.replace('flickr.com/people/%s/' % self.username,
                        'flickr.com/people/%s/' % self.key.id())
    return super(Flickr, self).canonicalize_url(url, **kwargs)


class AuthHandler(util.Handler):
  """Base OAuth handler for Flickr."""
  def start_oauth_flow(self, feature):
    starter = util.oauth_starter(
      oauth_flickr.StartHandler, feature=feature
    ).to(
      # TODO: delete instead of write. if we do that below, it works, and we get
      # granted delete permissions. however, if we then attempt to actually
      # delete something, it fails with code 99 "Insufficient permissions.
      # Method requires delete privileges; write granted." and
      # https://www.flickr.com/services/auth/list.gne shows that my user's
      # permissions for the Bridgy app are back to write, not delete. wtf?!
      '/flickr/add', scopes='write' if feature == 'publish' else 'read'
    )
    return starter(self.request, self.response).post()


class StartHandler(AuthHandler):
  """Custom handler to start Flickr auth process."""
  def post(self):
    return self.start_oauth_flow(self.request.get('feature'))


class AddFlickr(oauth_flickr.CallbackHandler, AuthHandler):
  """Custom handler to add Flickr source when auth completes.

  If this account was previously authorized with greater permissions, this will
  trigger another round of auth with elevated permissions.
  """
  def finish(self, auth_entity, state=None):
    logging.debug('finish with %s, %s', auth_entity, state)
    source = self.maybe_add_or_delete_source(Flickr, auth_entity, state)
    feature = util.decode_oauth_state(state).get('feature')
    if source and feature == 'listen' and 'publish' in source.features:
      # we had signed up previously with publish, so we'll reauth to
      # avoid losing that permission
      logging.info('Restarting OAuth flow to get publish permissions.')
      source.features.remove('publish')
      source.put()
      return self.start_oauth_flow('publish')


application = webapp2.WSGIApplication([
  ('/flickr/start', StartHandler),
  ('/flickr/add', AddFlickr),
  ('/flickr/delete/finish',
   oauth_flickr.CallbackHandler.to('/delete/finish')),
  ('/flickr/publish/start',
   oauth_flickr.StartHandler.to('/publish/flickr/finish')),
], debug=appengine_config.DEBUG)
