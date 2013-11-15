"""Facebook API code and datastore model classes.

Permissions needed:
read_stream for links posted by user
offline_access for, uh, offline access

graph explorer app_id: 145634995501895

echo 'SELECT username FROM user WHERE uid = me() OR username = "btaylor" OR uid IN (SELECT uid2 FROM friend WHERE uid1 = me()) LIMIT 3' | \
  curl "https://api.facebook.com/method/fql.query?access_token=...&format=json&query=`sed 's/ /%20/g'`"

add this to convert to a SQL INSERT statement:

example_data.sql

example link id: 252878954730164

import json, urllib2
TOKEN='[copy from graph api explorer]'
query = 'SELECT uid FROM group WHERE id = 13243224451'
json.loads(urllib2.urlopen(
  'https://api.facebook.com/method/fql.query?access_token=%s&query=%s&format=json' % (
    TOKEN, urllib2.quote(query))).read())

query = '''SELECT post_fbid, fromid, username, time, text FROM comment WHERE object_id IN \
           (SELECT link_id FROM link WHERE owner = 212038)
         ORDER BY time DESC'''

example output:

'[{"post_fbid":"146557492086023","fromid":212038,"username":"","time":1310359658,"text":"testing facebook api 2..."},{"post_fbid":"229626890404988","fromid":212038,"username":"","time":1310359075,"text":"testing facebook api..."}]'

snarfed.org uid is 212038
mobile uploads photo album aid is 2289690 (taken from html)


test users
===
# access token below is bridgy's app login
https://graph.facebook.com/256884317673197/accounts/test-users?&name=TestUser%20One&permissions=offline_access&method=post&access_token=...

# response
{
   "id": "100002841140165",
   "access_token": "..",
   "login_url": "https://www.facebook.com/platform/test_account_login.php?user_id=100002841140165&n=6gCGadkRXcAhY99",
   "email": "testuser_lymuziz_one\u0040tfbnw.net",
   "password": "1193027629"
}


echo 'SELECT id, name, url, pic, pic_square, pic_small, pic_big, type, username FROM profile WHERE id = 212038' | \
  curl "https://api.facebook.com/method/fql.query?access_token=...&format=json&query=`sed 's/ /%20/g'`"

TODO: use third_party_id if we ever need to store an fb user id anywhere else.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import collections
import datetime
import json
import logging
import pprint
import urllib
import urlparse

from activitystreams import facebook as as_facebook
from activitystreams.oauth_dropins import facebook as oauth_facebook
import appengine_config
import handlers
import models
import util

from google.appengine.api import urlfetch
from google.appengine.ext import db
import webapp2

FQL_URL = ('http://localhost:8000/fql' if appengine_config.MOCKFACEBOOK
           else 'https://graph.facebook.com/fql')


def fql(query, auth_entity):
  """Runs an FQL query.

  Args:
  query: string
  auth_entity: oauth_dropins.facebook.FacebookAuth

  Returns: dict, decoded JSON response

  TODO: error handling
  """
  logging.debug('Running FQL query "%s"', query)
  url = util.add_query_params(FQL_URL, {'q': query})
  resp = auth_entity.urlopen(url, timeout=999)
  assert resp.getcode() == 200, resp.getcode()

  data = resp.read()
  logging.debug('FQL response: %s', data)
  data = json.loads(data)

  # Facebook API error details:
  # https://developers.facebook.com/docs/reference/api/errors/
  if isinstance(data, dict) and data.get('error_code') in (102, 190):
    raise models.DisableSource()
  assert 'error_code' not in data and 'error_msg' not in data
  return data


class FacebookPage(models.Source):
  """A facebook profile or page.

  The key name is the facebook id.

  Attributes:
    comment_data: FQL results
    link_data: FQL results
    profile_data: FQL results
  """

  TYPE_NAME = 'Facebook'

  # full human-readable name
  name = db.StringProperty()
  picture = db.LinkProperty()
  type = db.StringProperty(choices=('user', 'page'))
  # unique name used in fb URLs, e.g. facebook.com/[username]
  username = db.StringProperty()

  # the token should be generated with the offline_access scope so that it
  # doesn't expire. details: http://developers.facebook.com/docs/authentication/
  auth_entity = db.ReferenceProperty(oauth_facebook.FacebookAuth)

  def display_name(self):
    return self.name

  @staticmethod
  def new(handler, auth_entity=None):
    """Creates and returns a FacebookPage for the logged in user.

    Args:
      handler: the current RequestHandler
      auth_entity: oauth_dropins.facebook.FacebookAuth
    """
    user = json.loads(auth_entity.user_json)
    id = (user['id'])
    picture='http://graph.facebook.com/%s/picture' % user.get('username', id)
    return FacebookPage(key_name=id,
                        owner=models.User.get_current_user(),
                        auth_entity=auth_entity,
                        picture=picture,
                        **user)

  def get_post(self, id):
    """Fetches a post.

    Example data:
      id: 212038_10100823411129293  [USER-ID]_[POST-ID]
      API URL: https://graph.facebook.com/212038_10100823411094363
      Permalinks:
        https://www.facebook.com/10100823411094363
        https://www.facebook.com/212038/posts/10100823411094363
        https://www.facebook.com/photo.php?fbid=10100823411094363

    Args:
      id: string, full post id (with user id prefix)

    Returns: dict, decoded ActivityStreams object, or None
    """
    fb = as_facebook.Facebook(self.auth_entity.access_token())
    count, activities = fb.get_activities(activity_id=id)
    return activities[0]['object'] if activities else None

  def get_comment(self, id):
    """Fetches a comment.

    Example data:
      id: 10100823411094363_10069288  [POST-ID]_[COMMENT-ID]
      API URL: https://graph.facebook.com/10100823411094363_10069288
      Permalink: https://www.facebook.com/10100823411094363&comment_id=10069288

    Args:
      id: string, full comment id (with post id prefix)

    Returns: dict, decoded ActivityStreams comment object, or None
    """
    return as_facebook.Facebook(self.auth_entity.access_token()).get_comment(id)

  def get_posts(self):
    """Returns list of (link id aka post object id, link url).
    """
    self.comment_data = self.fql(
      """SELECT post_fbid, time, fromid, username, object_id, text FROM comment
         WHERE object_id IN (SELECT link_id FROM link WHERE owner = %s)
         ORDER BY time DESC""" % self.key().name())

    link_ids = set(str(c['object_id']) for c in self.comment_data)
    self.link_data = self.fql('SELECT link_id, url FROM link WHERE link_id IN (%s)' %
                              ','.join(link_ids))

    fromids = set(str(c['fromid']) for c in self.comment_data)
    self.profile_data = self.fql(
      'SELECT id, name, url FROM profile WHERE id IN (%s)' % ','.join(fromids))

    return [(l['link_id'], l['url']) for l in self.link_data]

  def get_comments(self, posts_and_targets):
    comments_by_link_id = collections.defaultdict(list)
    for c in self.comment_data:
      comments_by_link_id[c['object_id']].append(c)

    profiles = dict((p['id'], p) for p in self.profile_data)
    links = dict((l['link_id'], l['url']) for l in self.link_data)

    comments = []
    for link_id, dest in posts_and_targets:
      for c in comments_by_link_id[link_id]:
        fromid = c['fromid']
        profile = profiles[fromid]
        post_url = 'https://www.facebook.com/permalink.php?story_fbid=%s&id=%s' % (
          c['object_id'], fromid)

        comments.append(FacebookComment(
            key_name=c['post_fbid'],
            source=self,
            dest=dest,
            source_post_url=post_url,
            dest_post_url=links[link_id],
            created=datetime.datetime.utcfromtimestamp(c['time']),
            author_name=profile['name'],
            author_url=profile['url'],
            content=c['text'],
            fb_fromid=fromid,
            fb_username=c['username'],
            fb_object_id=c['object_id'],
            ))

    return comments

  def fql(self, query):
    return fql(query, self.auth_entity)


class FacebookComment(models.Comment):
  """Key name is the comment's object_id.

  Most of the properties correspond to the columns of the content table in FQL.
  http://developers.facebook.com/docs/reference/fql/comment/
  """

  # user id who wrote the comment
  fb_fromid = db.IntegerProperty(required=True)

  # name entered by the user when they posted the comment. usually blank,
  # generally only populated for external users. if this is provided,
  # fb_fromid will be 0.
  fb_username = db.StringProperty()

  # id of the object this comment refers to
  fb_object_id = db.IntegerProperty(required=True)


class AddFacebookPage(oauth_facebook.CallbackHandler):
  messages = []

  def finish(self, auth_entity, state=None):
    FacebookPage.create_new(self, auth_entity=auth_entity)
    self.redirect('/')


class DeleteFacebookPage(util.Handler):
  def post(self):
    page = FacebookPage.get_by_key_name(self.request.params['key_name'])
    # TODO: remove tasks, etc.
    msg = 'Deleted %s source: %s' % (page.type_display_name(),
                                     page.display_name())
    page.delete()
    self.redirect('/?msg=' + msg)


application = webapp2.WSGIApplication([
    ('/facebook/start', oauth_facebook.StartHandler.to('/facebook/add')),
    ('/facebook/add', AddFacebookPage),
    ('/facebook/delete', DeleteFacebookPage),
    # e.g. http://localhost:8080/facebook/post/212038/10100823411094363
    ('/facebook/post/([^/]+)/([^/]+)',
     handlers.ObjectHandler.using(FacebookPage, 'get_post')),
    ('/facebook/comment/([^/]+)/([^/]+)',
     handlers.ObjectHandler.using(FacebookPage, 'get_comment')),
    ], debug=appengine_config.DEBUG)
