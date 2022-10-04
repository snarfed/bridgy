"""Browser extension views.
"""
import copy
import logging
from operator import itemgetter

from flask import jsonify, make_response, request
from flask.views import View
from google.cloud import ndb
from granary import as1
from oauth_dropins.webutil import flask_util
from oauth_dropins.webutil.util import json_dumps, json_loads

from flask_app import app
import models
from models import Activity, Domain, Source
import util

logger = logging.getLogger(__name__)

JSON_CONTENT_TYPE = 'application/json'

# See https://www.cloudimage.io/
IMAGE_PROXY_URL_BASE = 'https://aujtzahimq.cloudimg.io/v7/'


def merge_by_id(existing, updates):
  """Merges two lists of AS1 objects by id.

  Overwrites the objects in the existing list with objects in the updates list
  with the same id. Requires all objects to have ids.

  Args:
    existing: sequence of AS1 dicts
    updates: sequence of AS1 dicts

  Returns: merged list of AS1 dicts
  """
  objs = {o['id']: o for o in existing}
  objs.update({o['id']: o for o in updates})
  return sorted(objs.values(), key=itemgetter('id'))


class BrowserSource(Source):
  """A source whose data is provided by the browser extension.

  Current subclasses are Instagram and Facebook.
  """
  CAN_LISTEN = True
  CAN_PUBLISH = False
  AUTO_POLL = False

  # set by subclasses
  GR_CLASS = None
  OAUTH_START = None
  gr_source = None

  @classmethod
  def key_id_from_actor(cls, actor):
    """Returns the key id for this entity from a given AS1 actor.

    To be implemented by subclasses.

    Args:
      actor: dict AS1 actor

    Returns: str, key id to use for the corresponding datastore entity
    """
    raise NotImplementedError()

  @classmethod
  def new(cls, auth_entity=None, actor=None, **kwargs):
    """Creates and returns an entity based on an AS1 actor.

    Args:
      auth_entity: unused
      actor: dict AS1 actor
    """
    assert not auth_entity
    assert actor

    if not kwargs.get('features'):
      kwargs['features'] = ['listen']

    try:
      id = cls.key_id_from_actor(actor)
    except KeyError as e:
      flask_util.error(f'Missing AS1 actor field: {e}')

    src = cls(id=id,
              name=actor.get('displayName'),
              picture=actor.get('image', {}).get('url'),
              **kwargs)
    src.domain_urls, src.domains = src.urls_and_domains(None, None, actor=actor,
                                                        resolve_source_domain=False)
    return src

  @classmethod
  def button_html(cls, feature, **kwargs):
    return cls.OAUTH_START.button_html(
      '/about#browser-extension',
      form_method='get',
      image_prefix='/oauth_dropins_static/')

  def get_activities_response(self, *args, **kwargs):
    """Uses Activity entities stored in the datastore."""
    activities = []

    activity_id = kwargs.get('activity_id')
    if activity_id:
      activity = Activity.get_by_id(self.gr_source.tag_uri(activity_id))
      if activity:
        activities = [activity]
    else:
      activities = Activity.query(Activity.source == self.key)\
                           .order(-Activity.updated).fetch(50)

    activities = [json_loads(a.activity_json) for a in activities]
    for a in activities:
      as1.prefix_urls(a, 'image', IMAGE_PROXY_URL_BASE)

    return self.gr_source.make_activities_base_response(activities)

  def get_comment(self, comment_id, activity=None, **kwargs):
    """Uses the activity passed in the activity kwarg."""
    if activity:
      for reply in activity.get('object', {}).get('replies', {}).get('items', []):
        parsed = util.parse_tag_uri(reply.get('id', ''))
        if parsed and parsed[1] == comment_id:
          return reply

  def get_like(self, activity_user_id, activity_id, like_user_id, activity=None,
               **kwargs):
    """Uses the activity passed in the activity kwarg."""
    if activity:
      for tag in activity.get('object', {}).get('tags', []):
        if tag.get('verb') == 'like':
          parsed = util.parse_tag_uri(tag.get('author', {}).get('id', ''))
          if parsed and parsed[1] == like_user_id:
            return tag


class BrowserView(View):
  """Base class for requests from the browser extension."""
  def source_class(self):
    return models.sources.get(request.path.strip('/').split('/')[0])

  def gr_source(self):
    return self.source_class().gr_source

  def check_token(self, load_source=True):
    """Loads the token and checks that it has at least one domain registered.

    Expects token in the `token` query param.

    Raises: :class:`HTTPException` with HTTP 403 if the token is missing or
      invalid
    """
    token = request.values['token']
    domains = Domain.query(Domain.tokens == token).fetch()
    logging.info(f'Found domains for token {token}: {domains}')
    if not domains:
      self.error(f'No domains found for token {token}. Click Reconnect to Bridgy above to register your domain!', 403)

  def auth(self):
    """Checks token and loads and returns the source.

    Raises: :class:`HTTPException` with HTTP 400 or 403
    """
    self.check_token()
    return util.load_source(error_fn=self.error)

  @staticmethod
  def error(msg, status=400):
    """Return plain text errors for display in the browser extension."""
    flask_util.error(msg, status=status, response=make_response(
      msg, status, {'Content-Type': 'text/plain; charset=utf-8'}))


class Status(BrowserView):
  """Runs preflight checks for a source and returns status and config info.

  Response body is a JSON map with these fields:
    status: string, 'enabled' or 'disabled'
    poll-seconds: integer, current poll frequency for this source in seconds
  """
  def dispatch_request(self):
    source = self.auth()

    out = {
      'status': source.status,
      'poll-seconds': source.poll_period().total_seconds(),
    }
    logger.info(f'Returning {out}')
    return out


class Homepage(BrowserView):
  """Parses a silo home page and returns the logged in user's username.

  Request body is https://www.instagram.com/ HTML for a logged in user.
  """
  def dispatch_request(self):
    gr_src = self.gr_source()
    _, actor = gr_src.scraped_to_activities(request.get_data(as_text=True))
    logger.info(f'Got actor: {actor}')

    if actor:
      username = actor.get('username')
      if username:
        logger.info(f'Returning {username}')
        return jsonify(username)

    self.error(f"Scrape error: couldn't determine logged in {gr_src.NAME} user or username")


class Feed(BrowserView):
  """Parses a silo feed page and returns the posts.

  Request body is HTML from a silo profile with posts, eg
  https://www.instagram.com/name/ , for a logged in user.

  Response body is the JSON list of translated ActivityStreams activities.
  """
  def dispatch_request(self):
    self.auth()
    activities, _ = self.scrape()
    return jsonify(activities)

  def scrape(self):
    gr_src = self.gr_source()
    activities, actor = gr_src.scraped_to_activities(request.get_data(as_text=True))
    ids = ' '.join(a['id'] for a in activities)
    logger.info(f"Activities: {ids}")

    if activities and not any(as1.is_public(a) for a in activities):
      self.error(f'None of your recent {gr_src.NAME} posts are public. <a href="https://brid.gy/about#fully+public+posts">Bridgy only handles fully public posts.</a>')

    return activities, actor


class Profile(Feed):
  """Parses a silo profile page and creates or updates its Bridgy user.

  Request body is HTML from an IG profile, eg https://www.instagram.com/name/ ,
  for a logged in user.

  Response body is the JSON string URL-safe key of the Bridgy source entity.
  """
  def dispatch_request(self):
    _, actor = self.scrape()
    if not actor:
      actor = self.gr_source().scraped_to_actor(request.get_data(as_text=True))

    if not actor:
      self.error('Scrape error: missing actor!')

    if not as1.is_public(actor):
      self.error(f'Your {self.gr_source().NAME} account is private. Bridgy only supports public accounts.')

    self.check_token()

    # use temporary source instance to get only non-silo, non-blocklisted
    # profile URLs from actor
    src = self.source_class().new(actor=actor)
    actor.pop('url', None)
    actor['urls'] = [{'value': url} for url in src.domain_urls]

    # create/update the Bridgy account
    source = self.source_class().create_new(self, actor=actor)
    return jsonify(source.key.urlsafe().decode())


class Post(BrowserView):
  """Parses a silo post's HTML and creates or updates an Activity.

  Request body is HTML from a silo post, eg https://www.instagram.com/p/ABC123/

  Response body is the translated ActivityStreams activity JSON.
  """
  def dispatch_request(self):
    source = self.auth()

    gr_src = self.gr_source()
    new_activity, actor = gr_src.scraped_to_activity(request.get_data(as_text=True))
    if not new_activity:
      self.error(f'Scrape error: no {gr_src.NAME} post found in HTML')

    @ndb.transactional()
    def update_activity():
      id = new_activity.get('id')
      if not id:
        self.error('Scrape error: post missing id')
      activity = Activity.get_by_id(id)

      if activity:
        # we already have this activity! merge in any new comments and likes
        merged_activity = copy.deepcopy(new_activity)
        merged_obj = merged_activity.setdefault('object', {})
        existing_activity = json_loads(activity.activity_json)
        existing_obj = existing_activity.get('object', {})

        replies = merged_obj.setdefault('replies', {})
        as1.merge_by_id(replies, 'items',
                        existing_obj.get('replies', {}).get('items', []))
        replies['totalItems'] = len(replies.get('items', []))
        as1.merge_by_id(merged_obj, 'tags', existing_obj.get('tags', []))
        activity.activity_json = json_dumps(merged_activity)

      else:
        activity = Activity(id=id, source=source.key,
                            html=request.get_data(as_text=True),
                            activity_json=json_dumps(new_activity))

      # store and return the activity
      activity.put()
      logger.info(f"Stored activity {id}")

    update_activity()
    return new_activity


class Extras(BrowserView):
  """Merges extras (comments, reactions) from silo HTML into an existing Activity.

  Requires the request parameter `id` with the silo post's id (not shortcode!).

  Response body is the translated ActivityStreams JSON for the extras.

  Subclasses must populate the MERGE_METHOD constant with the string name of the
  granary source class's method that parses extras from silo HTML and merges
  them into an activity.
  """
  MERGE_METHOD = None

  def dispatch_request(self, *args):
    source = self.auth()

    gr_src = self.gr_source()
    id = request.values['id']

    # validate request
    parsed_id = util.parse_tag_uri(id)
    if not parsed_id:
      self.error(f'Scrape error: expected id to be tag URI; got {id}')

    activity = Activity.get_by_id(id)
    if not activity:
      self.error(f'No {gr_src.NAME} post found for id {id}', 404)
    elif activity.source != source.key:
      self.error(f'Activity {id} is owned by {activity.source}, not {source.key}', 403)

    activity_data = json_loads(activity.activity_json)

    # convert new extras to AS, merge into existing activity
    try:
      new_extras = getattr(gr_src, self.MERGE_METHOD)(
        request.get_data(as_text=True), activity_data)
    except ValueError as e:
      self.error(f"Scrape error: couldn't parse extras: {e}")

    activity.activity_json = json_dumps(activity_data)
    activity.put()

    extra_ids = ' '.join(c['id'] for c in new_extras)
    logger.info(f"Stored extras for activity {id}: {extra_ids}")
    return jsonify(new_extras)


class Comments(Extras):
  """Parses comments from silo HTML and adds them to an existing Activity.

  Requires the request parameter `id` with the silo post's id (not shortcode!).

  Response body is the translated ActivityStreams JSON for the comments.
  """
  MERGE_METHOD = 'merge_scraped_comments'


class Reactions(Extras):
  """Parses reactions/likes from silo HTML and adds them to an existing Activity.

  Requires the request parameter `id` with the silo post's id (not shortcode!).

  Response body is the translated ActivityStreams JSON for the reactions.
  """
  MERGE_METHOD = 'merge_scraped_reactions'


class Poll(BrowserView):
  """Triggers a poll for a browser-based account."""
  def dispatch_request(self):
    source = self.auth()
    util.add_poll_task(source, now=True)
    return jsonify('OK')


class TokenDomains(BrowserView):
  """Returns the domains that a token is registered for."""
  def dispatch_request(self):
    token = request.values['token']

    domains = [d.key.id() for d in Domain.query(Domain.tokens == token)]
    if not domains:
      indieauth_start = util.host_url(f'/indieauth/start?token={token}')
      self.error(f'Not connected to Bridgy. <a href="{indieauth_start}" target="_blank"">Connect now!</a>', 404)

    return jsonify(domains)


def route(source_cls):
  """Registers browser extension URL routes for a given source class.

  ...specifically, with the source's short name as the routes' URL prefix.
  """
  for route, cls in (
      (f'/{source_cls.SHORT_NAME}/browser/status', Status),
      (f'/{source_cls.SHORT_NAME}/browser/homepage', Homepage),
      (f'/{source_cls.SHORT_NAME}/browser/profile', Profile),
      (f'/{source_cls.SHORT_NAME}/browser/feed', Feed),
      (f'/{source_cls.SHORT_NAME}/browser/post', Post),
      (f'/{source_cls.SHORT_NAME}/browser/likes', Reactions),
      (f'/{source_cls.SHORT_NAME}/browser/comments', Comments),
      (f'/{source_cls.SHORT_NAME}/browser/reactions', Reactions),
      (f'/{source_cls.SHORT_NAME}/browser/poll', Poll),
      (f'/{source_cls.SHORT_NAME}/browser/token-domains', TokenDomains),
    ):
    app.add_url_rule(route, view_func=cls.as_view(route),
                     methods=['GET', 'POST'] if cls == Status else ['POST'])
