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
import logging
import re
import sys
import urllib2
import urlparse

import appengine_config

from activitystreams import facebook as as_facebook
from activitystreams.oauth_dropins import facebook as oauth_facebook
from activitystreams.source import SELF
import models
import util

from google.appengine.ext import ndb
from google.appengine.ext.webapp import template
import webapp2

API_PHOTOS = 'me/photos/uploaded'
# returns yes and maybe
API_USER_RSVPS = 'me/events'
API_USER_RSVPS_DECLINED = 'me/events/declined'
API_USER_RSVPS_NOT_REPLIED = 'me/events/not_replied'
# Ideally this fields arg would just be [default fields plus comments], but
# there's no way to ask for that. :/
# https://developers.facebook.com/docs/graph-api/using-graph-api/v2.1#fields
API_EVENT = '%s?fields=comments,description,end_time,id,likes,name,owner,picture,privacy,start_time,timezone,updated_time,venue'
API_EVENT_RSVPS = '%s/invited'


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

  @classmethod
  def lookup(cls, id):
    """Returns the entity with the given id or username."""
    return ndb.Key(cls, id).get() or cls.query(cls.username == id).get()

  def silo_url(self):
    """Returns the Facebook account URL, e.g. https://facebook.com/foo."""
    return self.as_source.user_url(self.username or self.key.id())

  def get_data(self, url):
    """Simple wrapper around as_source.urlopen() that returns 'data' list."""
    return self.as_source.urlopen(url).get('data', [])

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
      photos = self.get_data(API_PHOTOS)

      # also get events and RSVPs
      # https://developers.facebook.com/docs/graph-api/reference/user/events/
      # https://developers.facebook.com/docs/graph-api/reference/event#edges
      # TODO: also fetch and use API_USER_RSVPS_DECLINED
      user_rsvps = self.get_data(API_USER_RSVPS)

      # have to re-fetch the events because the user rsvps response doesn't
      # include the event description, which we need for original post links.
      events = [self.as_source.urlopen(API_EVENT % r['id'])
                for r in user_rsvps if r.get('id')]

      # also, only process events that the user is the owner of. avoids (but
      # doesn't prevent) processing big non-indieweb events with tons of
      # attendees that put us over app engine's instance memory limit. details:
      # https://github.com/snarfed/bridgy/issues/77
      events_and_rsvps = [(e, self.get_data(API_EVENT_RSVPS % e['id']))
                          for e in events
                          if e.get('owner', {}).get('id') == self.key.id()]

    except urllib2.HTTPError, e:
      # XXX TEMPORARY!!! TODO(ryan): remove after closing
      # https://github.com/snarfed/bridgy/issues/394
      if str(e.code) == '400':
        self.as_source.create_notification(
          self.key.id(),
          "Brid.gy's access to your account has expired. Click here to renew it now!",
          'https://www.brid.gy/facebook/start')
        raise models.DisableSource()

      # Facebook API error details:
      # https://developers.facebook.com/docs/graph-api/using-graph-api/#receiving-errorcodes
      # https://developers.facebook.com/docs/reference/api/errors/
      exc_type, exc_value, exc_traceback = sys.exc_info()
      try:
        body = json.loads(e.read())
      except:
        # response isn't JSON. ignore and re-raise the original exception
        raise exc_type, exc_value, exc_traceback

      error = body.get('error', {})
      if error.get('code') in (102, 190):
        subcode = error.get('error_subcode')
        if subcode == 458:  # revoked
          raise models.DisableSource()
        elif subcode in (463, 460):  # expired, changed password
          # ask the user to reauthenticate
          self.as_source.create_notification(
            self.key.id(),
            "Brid.gy's access to your account has expired. Click here to renew it now!",
            'https://www.brid.gy/facebook/start')
          raise models.DisableSource()

      # other error. re-raise original exception
      raise exc_type, exc_value, exc_traceback

    # add photos. they show up as both a post and a photo, each with a separate
    # id. the post's object_id field points to the photo's id. de-dupe by
    # switching the post to use the fb_object_id when it's provided.
    activities = resp.setdefault('items', [])
    activities_by_fb_id = {}
    for activity in activities:
      obj = activity.get('object', {})
      fb_id = obj.get('fb_object_id')
      if not fb_id:
        continue

      activities_by_fb_id[fb_id] = activity
      for x in activity, obj:
        parsed = util.parse_tag_uri(x.get('id', ''))
        if parsed:
          _, orig_id = parsed
          x['id'] = self.as_source.tag_uri(fb_id)
          x['url'] = x.get('url', '').replace(orig_id, fb_id)

    # merge comments and likes from existing photo objects, and add new ones.
    for photo in photos:
      photo_activity = self.as_source.post_to_activity(photo)
      existing = activities_by_fb_id.get(photo.get('id'))
      if existing:
        existing['object'].setdefault('replies', {}).setdefault('items', []).extend(
          photo_activity['object'].get('replies', {}).get('items', []))
        existing['object'].setdefault('tags', []).extend(
            [t for t in photo_activity['object'].get('tags', [])
             if t.get('verb') == 'like'])
      else:
        activities.append(photo_activity)

    # add events
    activities += [self.as_source.event_to_activity(e, rsvps=r)
                   for e, r in events_and_rsvps]

    # TODO: remove once we're confident in our id parsing. (i'm going to canary
    # with just a few users before i do it for everyone.)
    #
    # discard objects with ids with colons in them. Background:
    # https://github.com/snarfed/bridgy/issues/305
    def remove_bad_ids(objs, label):
      ret = []
      for o in objs:
        id = util.parse_tag_uri(o.get('id') or o.get('object', {}).get('id') or '')
        if id and ':' in id[1]:
          logging.warning('Cowardly ignoring %s with bad id: %s', label,  id[1])
        else:
          ret.append(o)
      return ret

    resp['items'] = remove_bad_ids(activities, 'activity')
    for activity in resp['items']:
      obj = activity.get('object', {})
      obj['tags'] = remove_bad_ids(obj.setdefault('tags', []), 'tag/like')
      replies = obj.get('replies', {})
      items = replies.get('items')
      if items:
        replies['items'] = remove_bad_ids(items, 'comment')
        replies['totalItems'] = len(replies['items'])

    return util.trim_nulls(resp)

  def canonicalize_syndication_url(self, url):
    """Facebook-specific standardization of syndicated urls. Canonical form is
    https://facebook.com/0123456789

    Args:
      url: a string, the url of the syndicated content

    Return:
      a string, the canonical form of the syndication url
    """
    parsed = urlparse.urlparse(url)

    if (parsed.path.endswith('/permalink.php') or
        parsed.path.endswith('/photo.php') or
        parsed.path.endswith('/photos.php')):
      params = urlparse.parse_qs(parsed.query)
      ids = params.get('story_fbid') or params.get('fbid')
      if ids:
        url = 'https://www.facebook.com/%s/posts/%s' % (self.key.id(), ids[0])

    if self.username:
      url = url.replace('facebook.com/%s/' % self.username,
                        'facebook.com/%s/' % self.key.id())

    # facebook always uses https and www
    return re.sub('^https?://(www\.)?facebook.com/', 'https://www.facebook.com/',
                  url)


class OAuthCallback(oauth_facebook.CallbackHandler, util.Handler):
  """OAuth callback handler."""
  def finish(self, auth_entity, state=None):
    if not auth_entity:
      self.maybe_add_or_delete_source(FacebookPage, auth_entity, state)
      return

    choices = [json.loads(auth_entity.user_json)] + json.loads(auth_entity.pages_json)
    vars = {
      'action': '/facebook/add',
      'state': state,
      'auth_entity_key': auth_entity.key.urlsafe(),
      'choices': choices,
      }
    logging.info('Rendering choose_facebook.html with %s', vars)

    self.response.headers['Content-Type'] = 'text/html'
    self.response.out.write(
      template.render('templates/choose_facebook.html', vars))


class AddFacebookPage(util.Handler):
  def post(self):
    state = util.get_required_param(self, 'state')
    id = util.get_required_param(self, 'id')

    auth_entity_key = util.get_required_param(self, 'auth_entity_key')
    auth_entity = ndb.Key(urlsafe=auth_entity_key).get()

    if id != auth_entity.key.id():
      auth_entity = auth_entity.for_page(id)
      auth_entity.put()

    self.maybe_add_or_delete_source(FacebookPage, auth_entity, state)


application = webapp2.WSGIApplication([
    # OAuth scopes are set in listen.html and publish.html
    ('/facebook/start', util.oauth_starter(oauth_facebook.StartHandler).to(
      '/facebook/oauth_handler')),
    ('/facebook/oauth_handler', OAuthCallback),
    ('/facebook/add', AddFacebookPage),
    ('/facebook/delete/finish', oauth_facebook.CallbackHandler.to('/delete/finish')),
    ('/facebook/publish/start', oauth_facebook.StartHandler.to(
      '/publish/facebook/finish')),
    ], debug=appengine_config.DEBUG)
