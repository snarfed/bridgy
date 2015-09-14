"""Flickr source and data model storage class
"""
__author__ = ['Kyle Mahan <kyle@kylewm.com>']


import appengine_config
import datetime
import json
import logging
import models
import util
import webapp2

from granary import flickr as gr_flickr
from granary.source import SELF
from oauth_dropins import flickr as oauth_flickr

from google.appengine.ext import ndb


class Flickr(models.Source):
  """A flickr account.

  The key name is the nsid
  """
  # Fetching comments and likes is extremely request-intensive, so let's dial
  # back the frequency for now.
  FAST_POLL = datetime.timedelta(minutes=60)

  GR_CLASS = gr_flickr.Flickr
  SHORT_NAME = 'flickr'

  # unique name optionally used in URLs instead of nsid (e.g.,
  # flickr.com/photos/username)
  username = ndb.StringProperty()

  @staticmethod
  def new(handler, auth_entity=None, **kwargs):
    """Creates and returns a Flickr for the logged in user.

    Args:
      handler: the current RequestHandler
      auth_entity: oauth_dropins.flickr.FlickrAuth
    """
    person = json.loads(auth_entity.user_json).get('person', {})
    return Flickr(
      id=person.get('nsid'),
      auth_entity=auth_entity.key,
      name=person.get('realname', {}).get('_content'),
      username=person.get('username', {}).get('_content'),
      picture='https://farm{}.staticflickr.com/{}/buddyicons/{}.jpg' .format(
        person.get('iconfarm'), person.get('iconserver'),
        person.get('nsid')),
      url=person.get('profileurl', {}).get('_content'),
      **kwargs)

  def silo_url(self):
    """Returns the Flickr account URL,
    e.g. https://www.flickr.com/people/foo/."""
    return self.url

  def user_tag_id(self):
    """Returns the tag URI for this source, e.g. 'tag:instagram.com:123456'."""
    return self.gr_source.tag_uri(self.username)

  def get_activities_response(self, *args, **kwargs):
    """Discard min_id because we still want new comments/likes on old
    photos."""
    kwargs.setdefault('group_id', SELF)
    if 'min_id' in kwargs:
      del kwargs['min_id']
    return self.gr_source.get_activities_response(*args, **kwargs)

  def canonicalize_syndication_url(self, url):
    if not url.endswith('/'):
      url = url + '/'
    if self.username:
      url = url.replace('flickr.com/photos/%s/' % self.username,
                        'flickr.com/photos/%s/' % self.key.id())
      url = url.replace('flickr.com/people/%s/' % self.username,
                        'flickr.com/people/%s/' % self.key.id())
    url = super(Flickr, self).canonicalize_syndication_url(
      url, scheme='https', subdomain='www.')
    return url


class AddFlickr(oauth_flickr.CallbackHandler, util.Handler):
  def finish(self, auth_entity, state=None):
    logging.debug('finish with %s, %s', auth_entity, state)
    self.maybe_add_or_delete_source(Flickr, auth_entity, state)


application = webapp2.WSGIApplication([
  ('/flickr/start', util.oauth_starter(oauth_flickr.StartHandler).to('/flickr/add')),
  ('/flickr/add', AddFlickr),
  ('/flickr/delete/finish', oauth_flickr.CallbackHandler.to('/delete/finish')),
], debug=appengine_config.DEBUG)
