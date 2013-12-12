"""Facebook API code and datastore model classes.

Permissions needed:
read_stream for links posted by user
offline_access for, uh, offline access

TODO: use third_party_id if we ever need to store an fb user id anywhere else.

Example post ID and links
  id: 212038_10100823411129293  [USER-ID]_[POST-ID]
  API URL: https://graph.facebook.com/212038_10100823411094363
  Permalinks:
    https://www.facebook.com/10100823411094363
    https://www.facebook.com/212038/posts/10100823411094363
    https://www.facebook.com/photo.php?fbid=10100823411094363
  Local handler path: /post/facebook/212038/10100823411094363

Example comment ID and links
  id: 10100823411094363_10069288  [POST-ID]_[COMMENT-ID]
  API URL: https://graph.facebook.com/10100823411094363_10069288
  Permalink: https://www.facebook.com/10100823411094363&comment_id=10069288
  Local handler path: /comment/facebook/212038/10100823411094363_10069288


ongoing research, many posts have different types w/different ids, so the same
post id isn't necessarily used for comments:

212038_10100826987043133
picture id
'type': 'photo'
'object_id': '10100826986998223'
url needs user id

10100826986998223
post
used as comment id prefix: 10100826987043133_10077197
may also have user id: 212038_10100826987043133_10077197
no field with picture id
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import json

from activitystreams import facebook as as_facebook
from activitystreams.oauth_dropins import facebook as oauth_facebook
from activitystreams.source import SELF
import appengine_config
import models
import util

from google.appengine.ext import db
import webapp2


class FacebookPage(models.Source):
  """A facebook profile or page.

  The key name is the facebook id.
  """

  DISPLAY_NAME = 'Facebook'
  SHORT_NAME = 'facebook'

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
    url = 'http://facebook.com/' + id
    picture = 'http://graph.facebook.com/%s/picture' % user.get('username', id)
    return FacebookPage(key_name=id, auth_entity=auth_entity, picture=picture,
                        url=url, **user) # **user populates type, name, username

  def __init__(self, *args, **kwargs):
    super(FacebookPage, self).__init__(*args, **kwargs)
    if self.auth_entity:
      self.as_source = as_facebook.Facebook(self.auth_entity.access_token())

  def get_activities(self, fetch_replies=False, **kwargs):
    posts = self.as_source.get_activities(
      group_id=SELF, user_id=self.key().name(), **kwargs)[1]

    # also get uploaded photos manually since facebook sometimes collapses
    # multiple photos into albums, and the album post object won't have the post
    # content, comments, etc. from the individual photo posts.
    # http://stackoverflow.com/questions/12785120
    resp = self.as_source.urlread('https://graph.facebook.com/me/photos/uploaded')
    photos = [self.as_source.post_to_activity(p)
              for p in json.loads(resp).get('data', [])]

    return posts + photos

    # TODO: handle errors. (activitystreams-unofficial doesn't yet handle *or*
    # expose them.
    # Facebook API error details:
    # https://developers.facebook.com/docs/reference/api/errors/
    # if isinstance(data, dict) and data.get('error_code') in (102, 190):
    #   raise models.DisableSource()
    # assert 'error_code' not in data and 'error_msg' not in data


class AddFacebookPage(oauth_facebook.CallbackHandler):
  messages = set()

  def finish(self, auth_entity, state=None):
    fb = FacebookPage.create_new(self, auth_entity=auth_entity)
    util.added_source_redirect(self, fb)


application = webapp2.WSGIApplication([
    ('/facebook/start', oauth_facebook.StartHandler.to('/facebook/add')),
    ('/facebook/add', AddFacebookPage),
    ('/facebook/delete/finish', oauth_facebook.CallbackHandler.to('/delete/finish')),
    ], debug=appengine_config.DEBUG)
