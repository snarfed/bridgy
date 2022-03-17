"""Flickr source and data model storage class."""
import datetime
import logging
import models
import util

from flask import request
from google.cloud import ndb
from granary import flickr as gr_flickr
from granary.source import SELF
from oauth_dropins import flickr as oauth_flickr
from oauth_dropins.webutil.util import json_dumps, json_loads

from flask_app import app

logger = logging.getLogger(__name__)


class Flickr(models.Source):
  """A Flickr account.

  The key name is the nsid.
  """
  # Fetching comments and likes is extremely request-intensive, so let's dial
  # back the frequency for now.
  FAST_POLL = datetime.timedelta(minutes=60)
  GR_CLASS = gr_flickr.Flickr
  OAUTH_START = oauth_flickr.Start
  SHORT_NAME = 'flickr'
  TRANSIENT_ERROR_HTTP_CODES = ('400',)
  CAN_PUBLISH = True
  URL_CANONICALIZER = util.UrlCanonicalizer(
    domain=GR_CLASS.DOMAIN,
    approve=r'https://www\.flickr\.com/(photos|people)/[^/?]+/([^/?]+/)?$',
    reject=r'https://login\.yahoo\.com/.*',
    subdomain='www',
    trailing_slash=True)

  # unique name optionally used in URLs instead of nsid (e.g.,
  # flickr.com/photos/username)
  username = ndb.StringProperty()

  @staticmethod
  def new(auth_entity=None, **kwargs):
    """Creates and returns a :class:`Flickr` for the logged in user.

    Args:
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
      picture='https://farm{iconfarm}.staticflickr.com/{iconserver}/buddyicons/{nsid}.jpg'.format(**person),
      url=person.get('profileurl', {}).get('_content'),
      **kwargs)

  def silo_url(self):
    """Returns the Flickr account URL, e.g. https://www.flickr.com/people/foo/."""
    return self.url

  def user_tag_id(self):
    """Returns the tag URI for this source, e.g. 'tag:flickr.com:123456'."""
    return self.gr_source.tag_uri(self.username)

  def label_name(self):
    """Human-readable name, username, or id for this source."""
    return self.name or self.username or self.key_id()

  def get_activities_response(self, *args, **kwargs):
    """Discard min_id because we still want new comments/likes on old photos."""
    kwargs.setdefault('group_id', SELF)
    if 'min_id' in kwargs:
      del kwargs['min_id']
    kwargs['count'] = min(10, kwargs.get('count', 0))
    return self.gr_source.get_activities_response(*args, **kwargs)

  def canonicalize_url(self, url, activity=None, **kwargs):
    if not url.endswith('/'):
      url = url + '/'
    if self.username:
      url = url.replace(f'flickr.com/photos/{self.username}/',
                        f'flickr.com/photos/{self.key_id()}/')
      url = url.replace(f'flickr.com/people/{self.username}/',
                        f'flickr.com/people/{self.key_id()}/')
    return super().canonicalize_url(url, **kwargs)


class AuthHandler():
  """Base OAuth handler for Flickr."""
  def start_oauth_flow(self, feature):
    starter = util.oauth_starter(oauth_flickr.Start, feature=feature)(
      # TODO: delete instead of write. if we do that below, it works, and we get
      # granted delete permissions. however, if we then attempt to actually
      # delete something, it fails with code 99 "Insufficient permissions.
      # Method requires delete privileges; write granted." and
      # https://www.flickr.com/services/auth/list.gne shows that my user's
      # permissions for the Bridgy app are back to write, not delete. wtf?!
      '/flickr/add', scopes='write' if feature == 'publish' else 'read')
    return starter.dispatch_request()


class Start(oauth_flickr.Start, AuthHandler):
  """Custom handler to start Flickr auth process."""
  def dispatch_request(self):
    return self.start_oauth_flow(request.form.get('feature'))


class AddFlickr(oauth_flickr.Callback, AuthHandler):
  """Custom handler to add Flickr source when auth completes.

  If this account was previously authorized with greater permissions, this will
  trigger another round of auth with elevated permissions.
  """
  def finish(self, auth_entity, state=None):
    logger.debug(f'finish with {auth_entity}, {state}')
    source = util.maybe_add_or_delete_source(Flickr, auth_entity, state)
    feature = util.decode_oauth_state(state).get('feature')
    if source and feature == 'listen' and 'publish' in source.features:
      # we had signed up previously with publish, so we'll reauth to
      # avoid losing that permission
      logger.info('Restarting OAuth flow to get publish permissions.')
      source.features.remove('publish')
      source.put()
      return self.start_oauth_flow('publish')


app.add_url_rule('/flickr/start', view_func=Start.as_view('flickr_start', '/flickr/add'), methods=['POST'])
app.add_url_rule('/flickr/add', view_func=AddFlickr.as_view('flickr_add', 'unused'))
app.add_url_rule('/flickr/delete/finish', view_func=oauth_flickr.Callback.as_view('flickr_delete_finish', '/delete/finish'))
app.add_url_rule('/flickr/publish/start', view_func=oauth_flickr.Start.as_view('flickr_publish_start', '/publish/flickr/finish'), methods=['POST'])
