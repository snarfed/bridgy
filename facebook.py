"""Facebook API code and datastore model classes.

TODO: use third_party_id if we ever need to store an FB user id anywhere else.

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
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import json
import urllib2

import appengine_config

from activitystreams import facebook as as_facebook
from activitystreams.oauth_dropins import facebook as oauth_facebook
from activitystreams.source import SELF
import logging
import models
import urllib
import urllib2
import util

from google.appengine.ext import ndb
import webapp2

API_PHOTOS_URL = 'https://graph.facebook.com/me/photos/uploaded'
API_USER_RSVPS_URL = 'https://graph.facebook.com/me/events'  # returns yes and maybe
API_USER_RSVPS_DECLINED_URL = 'https://graph.facebook.com/me/events/declined'
API_USER_RSVPS_NOT_REPLIED_URL = 'https://graph.facebook.com/me/events/not_replied'
API_EVENT_RSVPS_URL = 'https://graph.facebook.com/%s/invited'
API_NOTIFICATION_URL = 'https://graph.facebook.com/%s/notifications'


class FacebookPage(models.Source):
  """A facebook profile or page.

  The key name is the facebook id.
  """

  AS_CLASS = as_facebook.Facebook
  SHORT_NAME = 'facebook'

  type = ndb.StringProperty(choices=('user', 'page'))
  # unique name used in fb URLs, e.g. facebook.com/[username]
  username = ndb.StringProperty()

  @staticmethod
  def new(handler, auth_entity=None, **kwargs):
    """Creates and returns a FacebookPage for the logged in user.

    Args:
      handler: the current RequestHandler
      auth_entity: oauth_dropins.facebook.FacebookAuth
      kwargs: property values
    """
    user = json.loads(auth_entity.user_json)
    as_source = as_facebook.Facebook(auth_entity.access_token())
    actor = as_source.user_to_actor(user)
    return FacebookPage(id=user['id'], type=user.get('type'),
                        auth_entity=auth_entity.key,
                        name=actor.get('displayName'),
                        username=actor.get('username'),
                        picture=actor.get('image', {}).get('url'),
                        url=actor.get('url'),
                        **kwargs)

  def get(self, url):
    """Simple wrapper around urlopen(). Returns decoded JSON dict."""
    return json.loads(self.as_source.urlopen(url).read())

  def get_data(self, url):
    """Variant of get() that returns 'data' list."""
    return self.get(url).get('data', [])

  def get_activities_response(self, **kwargs):
    # TODO: use batch API to get photos, events, etc in one request
    # https://developers.facebook.com/docs/graph-api/making-multiple-requests
    try:
      resp = self.as_source.get_activities_response(group_id=SELF, **kwargs)

      # also get uploaded photos manually since facebook sometimes collapses
      # multiple photos into albums, and the album post object won't have the
      # post content, comments, etc. from the individual photo posts.
      # http://stackoverflow.com/questions/12785120
      #
      # TODO: save and use ETag for all of these extra calls
      photos = self.get_data(API_PHOTOS_URL)

      # also get events and RSVPs
      # https://developers.facebook.com/docs/graph-api/reference/user/events/
      # https://developers.facebook.com/docs/graph-api/reference/event#edges
      # TODO: also fetch and use API_USER_RSVPS_DECLINED_URL
      user_rsvps = self.get_data(API_USER_RSVPS_URL)

      # have to re-fetch the events because the user rsvps response doesn't
      # include the event description, which we need for original post links.
      events = [self.get(as_facebook.API_OBJECT_URL % r['id'])
                for r in user_rsvps if r.get('id')]

      # also, only process events that the user is the owner of. avoids (but
      # doesn't prevent) processing big non-indieweb events with tons of
      # attendees that put us over app engine's instance memory limit. details:
      # https://github.com/snarfed/bridgy/issues/77
      events_and_rsvps = [(e, self.get_data(API_EVENT_RSVPS_URL % e['id']))
                          for e in events
                          if e.get('owner', {}).get('id') == self.key.id()]

    except urllib2.HTTPError, e:
      # Facebook API error details:
      # https://developers.facebook.com/docs/graph-api/using-graph-api/#receiving-errorcodes
      # https://developers.facebook.com/docs/reference/api/errors/
      try:
        body = json.loads(e.read())
        error = body.get('error', {})
        if error.get('code') in (102, 190):
          subcode = error.get('error_subcode')
          if subcode == 458:  # revoked
            raise models.DisableSource()
          elif subcode in (463, 460):  # expired, changed password
            self.notify_expired()
            raise models.DisableSource()
      except:
        # ignore and re-raise the original exception
        pass
      raise

    items = resp.setdefault('items', [])
    items += [self.as_source.post_to_activity(p) for p in photos]
    items += [self.as_source.event_to_activity(e, rsvps=r)
              for e, r in events_and_rsvps]
    return resp

  def notify_expired(self):
    """Sends the user a Facebook notification that asks them to reauthenticate.

    Uses the Notifications API (beta):
    https://developers.facebook.com/docs/games/notifications/#impl

    Raises: urllib2.HTPPError
    """
    logging.info('Facebook access token expired! Sending notification to user.')
    params = {
      'template': "Brid.gy's access to your account has expired. Click here to renew it now!",
      'href': 'https://www.brid.gy/facebook/start',
      # this is a synthetic app access token.
      # https://developers.facebook.com/docs/facebook-login/access-tokens/#apptokens
      'access_token': '%s|%s' % (appengine_config.FACEBOOK_APP_ID,
                                 appengine_config.FACEBOOK_APP_SECRET),
      }
    url = API_NOTIFICATION_URL % self.key.id()
    resp = urllib2.urlopen(urllib2.Request(url, data=urllib.urlencode(params)),
                           timeout=appengine_config.HTTP_TIMEOUT)
    logging.info('Response: %s %s' % (resp.getcode(), resp.read()))


class AddFacebookPage(oauth_facebook.CallbackHandler, util.Handler):
  def finish(self, auth_entity, state=None):
    self.maybe_add_or_delete_source(FacebookPage, auth_entity, state)


application = webapp2.WSGIApplication([
    # OAuth scopes are set in listen.html and publish.html
    ('/facebook/start', oauth_facebook.StartHandler.to('/facebook/add')),
    ('/facebook/add', AddFacebookPage),
    ('/facebook/delete/finish', oauth_facebook.CallbackHandler.to('/delete/finish')),
    ], debug=appengine_config.DEBUG)
