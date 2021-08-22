"""Meetup API code and datastore model classes.
"""
import logging

from granary import meetup as gr_meetup
from oauth_dropins import meetup as oauth_meetup
from oauth_dropins.webutil.util import json_dumps, json_loads

from models import Source
import util

# We don't support listen
LISTEN_SCOPES = []
PUBLISH_SCOPES = [
  'rsvp',
]


class Meetup(Source):
  GR_CLASS = gr_meetup.Meetup
  OAUTH_START = oauth_meetup.Start
  SHORT_NAME = 'meetup'
  BACKFEED_REQUIRES_SYNDICATION_LINK = True
  CAN_LISTEN = False
  CAN_PUBLISH = True
  URL_CANONICALIZER = util.UrlCanonicalizer(
    domain=GR_CLASS.DOMAIN,
    headers=util.REQUEST_HEADERS)

  @staticmethod
  def new(auth_entity=None, **kwargs):
    """Creates and returns a :class:`Meetup` for the logged in user.

    Args:
      auth_entity: :class:`oauth_dropins.meetup.MeetupAuth`
      kwargs: property values
    """
    user = json_loads(auth_entity.user_json)
    gr_source = gr_meetup.Meetup(access_token=auth_entity.access_token())
    actor = gr_source.user_to_actor(user)
    return Meetup(id=auth_entity.key.id(),
                  auth_entity=auth_entity.key,
                  name=actor.get('displayName'),
                  picture=actor.get('image', {}).get('url'),
                  url=actor.get('url'),
                  **kwargs)

  def silo_url(self):
    """Returns the Meetup account URL, e.g. https://meetup.com/members/...."""
    return self.gr_source.user_url(self.key.id())

  def label_name(self):
    """Returns the username."""
    return self.name


class Callback(oauth_meetup.Callback):
  def finish(self, auth_entity, state=None):
    util.maybe_add_or_delete_source(Meetup, auth_entity, state)


app.add_url_rule('/meetup/start', view_func=util.oauth_starter(oauth_meetup.Start).as_view('meetup_start', '/meetup/add', scopes=PUBLISH_SCOPES)) # we don't support listen
app.add_url_rule('/meetup/add', view_func=Callback.as_view('meetup_add'))
app.add_url_rule('/meetup/delete/finish', view_func=oauth_meetup.Callback.as_view('meetup_delete_finish', '/delete/finish'))
app.add_url_rule('/meetup/publish/start', view_func=oauth_meetup.Start.as_view('meetup_publish_finish', '/meetup/publish/finish', scopes=PUBLISH_SCOPES))
