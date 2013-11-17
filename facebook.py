"""Facebook API code and datastore model classes.

Permissions needed:
read_stream for links posted by user
offline_access for, uh, offline access

TODO: use third_party_id if we ever need to store an fb user id anywhere else.

Example post data
  id: 212038_10100823411129293  [USER-ID]_[POST-ID]
  API URL: https://graph.facebook.com/212038_10100823411094363
  Permalinks:
    https://www.facebook.com/10100823411094363
    https://www.facebook.com/212038/posts/10100823411094363
    https://www.facebook.com/photo.php?fbid=10100823411094363

Example comment data
  id: 10100823411094363_10069288  [POST-ID]_[COMMENT-ID]
  API URL: https://graph.facebook.com/10100823411094363_10069288
  Permalink: https://www.facebook.com/10100823411094363&comment_id=10069288

Extra properties stored in ActivityStreams comments returned by get_comments()
  TODO: remove this entirely? do we not need these at all?
  # user id who wrote the comment
  fb_fromid = db.IntegerProperty(required=True)

  # name entered by the user when they posted the comment. usually blank,
  # generally only populated for external users. if this is provided,
  # fb_fromid will be 0.
  fb_username = db.StringProperty()

  # id of the object this comment refers to
  fb_object_id = db.IntegerProperty(required=True)
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import itertools
import json
import logging

from activitystreams import facebook as as_facebook
from activitystreams.oauth_dropins import facebook as oauth_facebook
import appengine_config
import handlers
import models
import util

from google.appengine.ext import db
import webapp2


class FacebookPage(models.Source):
  """A facebook profile or page.

  The key name is the facebook id.

  Attributes:
    comment_data: FQL results
    link_data: FQL results
    profile_data: FQL results
  """

  TYPE_NAME = 'Facebook'

  type = db.StringProperty(choices=('user', 'page'))
  # unique name used in fb URLs, e.g. facebook.com/[username]
  username = db.StringProperty()

  @staticmethod
  def new(handler, auth_entity=None):
    """Creates and returns a FacebookPage for the logged in user.

    Args:
      handler: the current RequestHandler
      auth_entity: oauth_dropins.facebook.FacebookAuth
    """
    user = json.loads(auth_entity.user_json)
    id = user['id']
    picture='http://graph.facebook.com/%s/picture' % user.get('username', id)
    return FacebookPage(key_name=id, auth_entity=auth_entity, picture=picture,
                        **user)

  def __init__(*args, **kwargs):
    super(FacebookPage, self).__init__(*args, **kwargs)
    if self.auth_entity:
      self.as_source = as_facebook.Facebook(self.auth_entity.access_token())

  def display_name(self):
    return self.name

  def get_comments(self):
    return itertools.chain(*(a.get('replies', {}).get('items', [])
                             for a in self.as_source.get_activities()))

    # TODO: handle errors. (activitystreams-unofficial doesn't yet handle *or*
    # expose them.
    # Facebook API error details:
    # https://developers.facebook.com/docs/reference/api/errors/
    # if isinstance(data, dict) and data.get('error_code') in (102, 190):
    #   raise models.DisableSource()
    # assert 'error_code' not in data and 'error_msg' not in data


class AddFacebookPage(oauth_facebook.CallbackHandler):
  messages = []

  def finish(self, auth_entity, state=None):
    FacebookPage.create_new(self, auth_entity=auth_entity)
    self.redirect('/')


application = webapp2.WSGIApplication([
    ('/facebook/start', oauth_facebook.StartHandler.to('/facebook/add')),
    ('/facebook/add', AddFacebookPage),
    ('/facebook/post/([^/]+)/([^/]+)',
     handlers.ObjectHandler.using(FacebookPage, 'get_post')),
    ('/facebook/comment/([^/]+)/([^/]+)',
     handlers.ObjectHandler.using(FacebookPage, 'get_comment')),
    ], debug=appengine_config.DEBUG)
