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

import datetime
import logging
import json
import pprint
import urllib
import urlparse

import appengine_config
import models
import tasks
import util

from google.appengine.api import taskqueue
from google.appengine.api import urlfetch
from google.appengine.api import users
from google.appengine.ext import db
from google.appengine.ext import webapp
from google.appengine.ext.webapp.util import run_wsgi_app

# facebook api url templates. can't (easily) use urllib.urlencode() because i
# want to keep the %(...)s placeholders as is and fill them in later in code.
GET_AUTH_CODE_URL = '&'.join((
    'https://www.facebook.com/dialog/oauth/?',
    'scope=read_stream,offline_access',
    'client_id=%(client_id)s',
    # redirect_uri here must be the same in the access token request!
    'redirect_uri=%(host_url)s/facebook/got_auth_code',
    'response_type=code',
    'state=%(state)s',
    ))

GET_ACCESS_TOKEN_URL = '&'.join((
    'https://graph.facebook.com/oauth/access_token?',
    'client_id=%(client_id)s',
    # redirect_uri here must be the same in the oauth request!
    # (the value here doesn't actually matter since it's requested server side.)
    'redirect_uri=%(host_url)s/facebook/got_auth_code',
    'client_secret=%(client_secret)s',
    'code=%(auth_code)s',
    ))

FQL_URL = '&'.join((
    'https://api.facebook.com/method/fql.query?',
    'access_token=%(access_token)s',
    'format=json',
    'query=%(query)s',
    ))


class FacebookPage(models.Source):
  """A facebook profile or page.

  The key name is the facebook id.
  """

  TYPE_NAME = 'Facebook'

  # full human-readable name
  name = db.StringProperty()
  pic_small = db.LinkProperty()
  type = db.StringProperty(choices=('user', 'page'))
  # unique name used in fb URLs, e.g. facebook.com/[username]
  username = db.StringProperty()

  # the token should be generated with the offline_access scope so that it
  # doesn't expire. details: http://developers.facebook.com/docs/authentication/
  access_token = db.StringProperty()

  def display_name(self):
    """Returns name.
    """
    return self.name

  def type_display_name(self):
    return self.TYPE_NAME

  def fql(self, query):
    return FacebookApp.get().fql(query, self.access_token)

  @staticmethod
  def new(access_token, handler):
    """Creates and saves a FacebookPage for the logged in user.

    Args:
      access_token: string
      handler: the current webapp.RequestHandler

    Returns: FacebookPage

    # TODO: unify with WordPressSite, etc
    """
    results = FacebookApp.get().fql(
      'SELECT id, name, url, pic_small, type, username FROM profile WHERE id = me()',
      access_token)
    result = results[0]
    id = str(result['id'])
    existing = FacebookPage.get_by_key_name(id)
    page = FacebookPage(key_name=id, access_token=access_token, **result)

    if existing:
      logging.warning('Overwriting FacebookPage %s! Old version:\n%s' %
                      (id, page.to_xml()))
      handler.messages.append('Updated existing %s page: %s' %
                              (existing.type_display_name(), existing.display_name()))
    else:
      handler.messages.append('Added %s page: %s' %
                              (page.type_display_name(), page.display_name()))

    # TODO: ugh, *all* of this should be transactional
    page.save()
    models.User.get_current_user().add_source(page)
    taskqueue.add(name=tasks.Poll.make_task_name(page), queue_name='poll')
    return page

  def poll(self):
    # TODO: generic and expand beyond WordPressSite.
    # GQL so i don't have to import wordpress
    dests = db.GqlQuery('SELECT * FROM WordPressSite').fetch(100)
    comments = []

    query = """SELECT post_fbid, time, fromid, username, object_id, text FROM comment
               WHERE object_id IN (SELECT link_id FROM link WHERE owner = %s)
               ORDER BY time""" % self.key().name()
    comment_data = self.fql(query)

    link_ids = [str(c['object_id']) for c in comment_data]
    link_data = self.fql('SELECT link_id, url FROM link WHERE link_id IN (%s)' %
                       ','.join(link_ids))
    links = dict((l['link_id'], l['url']) for l in link_data)

    # TODO: cache?
    fromids = [str(c['fromid']) for c in comment_data]
    profile_data = self.fql(
      'SELECT id, name, url FROM profile WHERE id IN (%s)' % ','.join(fromids))
    profiles = dict((p['id'], p) for p in profile_data)

    for c in comment_data:
      link = links[c['object_id']]
      logging.debug('Looking for destination for link: %s' % link)

      # TODO: move rest of method to tasks!

      # look for destinations whose url contains this link. should be at most one.
      # (can't use this prefix code because we want the property that's a prefix
      # of the filter value, not vice versa.)
      # query = db.GqlQuery(
      #   'SELECT * FROM WordPressSite WHERE url = :1 AND url <= :2',
      #   link, link + u'\ufffd')
      dest = [d for d in dests if link.startswith(d.url)]
      assert len(dest) <= 1

      if dest:
        dest = dest[0]
        logging.debug('Found destination: %s' % dest.key().name())

        fromid = c['fromid']
        profile = profiles[fromid]
        post_url = 'https://www.facebook.com/permalink.php?story_fbid=%s&id=%s' % (
          c['object_id'], fromid)

        comments.append(FacebookComment(
            key_name=c['post_fbid'],
            source=self,
            dest=dest,
            source_post_url=post_url,
            dest_post_url=link,
            author_name=profile['name'],
            author_url=profile['url'],
            created=datetime.datetime.utcfromtimestamp(c['time']),
            content=c['text'],
            fb_fromid=fromid,
            fb_username=c['username'],
            fb_object_id=c['object_id'],
            ))

    return comments


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


class FacebookApp(db.Model):
  """Stores the bridgy app credentials that we use with the API."""
  app_id = db.StringProperty(required=True)
  app_secret = db.StringProperty(required=True)

  # this will be cached in the runtime
  __singleton = None

  @classmethod
  def get(cls):
    if not cls.__singleton:
      FacebookApp(app_id='160518970686787', app_secret='c9c676d47cf134a741b9744e6a4828c7').save()
      # TODO: check that there's only one
      cls.__singleton = cls.all().get()
    return cls.__singleton

  def fql(self, query, access_token):
    """Runs an FQL query.

    Args:
      access_token: string

    Returns: string

    TODO: error handling
    """
    assert access_token

    logging.debug('Running FQL query "%s" with access token %s', query, access_token)
    args = {
      'access_token': access_token,
      'query': urllib.quote(query),
      }
    resp = urlfetch.fetch(FQL_URL % args, deadline=999)
    assert resp.status_code == 200, resp.status_code
    data = json.loads(resp.content)
    logging.debug('FQL response: %s', pprint.pformat(data))
    assert 'error_code' not in data and 'error_msg' not in data
    return data

  def get_access_token(self, handler, redirect_uri):
    """Gets an access token for the current user.

    Actually just gets the auth code and redirects to /facebook_got_auth_code,
    which makes the next request to get the access token.

    Args:
      handler: the current webapp.RequestHandler
      redirect_uri: string, the local url to redirect to. Must begin with /.
    """
    assert self.app_id
    assert self.app_secret
    assert redirect_uri.startswith('/'), '%s does not start with /' % redirect_uri

    url = GET_AUTH_CODE_URL % {
      'client_id': self.app_id,
      # TODO: CSRF protection identifier.
      # http://developers.facebook.com/docs/authentication/
      'host_url': handler.request.host_url,
      'state': handler.request.host_url + redirect_uri,
      # 'state': urllib.quote(json.dumps({'redirect_uri': redirect_uri})),
      }
    handler.redirect(url)

  def _get_access_token_with_auth_code(self, handler, auth_code, redirect_uri):
    """Gets an access token based on an auth code.

    Args:
      handler: the current webapp.RequestHandler
      auth_code: string
      redirect_uri: string, the local url to redirect to. Must begin with /.
    """
    assert auth_code

    redirect_uri = urllib.unquote(redirect_uri)
    # assert redirect_uri.startswith('http://localhost:8080/'), redirect_uri
    assert '?' not in redirect_uri

    # TODO: handle permission declines, errors, etc
    url = GET_ACCESS_TOKEN_URL % {
      'auth_code': auth_code,
      'client_id': self.app_id,
      'client_secret': self.app_secret,
      'host_url': handler.request.host_url,
      }
    resp = urlfetch.fetch(url, deadline=999)
    # TODO: error handling. handle permission declines, errors, etc
    logging.debug('access token response: %s' % resp.content)
    params = urlparse.parse_qs(resp.content)
    access_token = params['access_token'][0]

    url = '%s?access_token=%s' % (redirect_uri, access_token)
    handler.redirect(url)


class AddFacebookPage(util.Handler):
  def post(self):
    FacebookApp.get().get_access_token(self, '/facebook/got_access_token')


class DeleteFacebookPage(util.Handler):
  def post(self):
    page = FacebookPage.get_by_key_name(self.request.params['name'])
    # TODO: remove tasks, etc.
    msg = 'Deleted %s source: %s' % (page.type_display_name(),
                                     page.display_name())
    page.delete()
    self.redirect('/?msg=' + msg)


class GotAuthCode(util.Handler):
  def get(self):
    FacebookApp.get()._get_access_token_with_auth_code(
      self, self.request.params['code'], self.request.params['state'])
    

class GotAccessToken(util.Handler):
  def get(self):
    access_token = self.request.params['access_token']
    page = FacebookPage.new(access_token, self)
    self.redirect('/')


application = webapp.WSGIApplication([
    ('/facebook/add', AddFacebookPage),
    ('/facebook/delete', DeleteFacebookPage),
    ('/facebook/got_auth_code', GotAuthCode),
    ('/facebook/got_access_token', GotAccessToken),
    ], debug=appengine_config.DEBUG)

def main():
  run_wsgi_app(application)


if __name__ == '__main__':
  main()
