"""Browser extension views.
"""
import copy
import logging
from operator import itemgetter

from flask import jsonify, request
from flask.views import View
from google.cloud import ndb
from granary import microformats2
from granary import source as gr_source
from oauth_dropins.webutil.flask_util import error
from oauth_dropins.webutil.util import json_dumps, json_loads

from flask_app import app
import models
from models import Activity, Domain, Source
import util

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

    src = cls(id=cls.key_id_from_actor(actor),
              name=actor.get('displayName'),
              picture=actor.get('image', {}).get('url'),
              **kwargs)
    src.domain_urls, src.domains = src._urls_and_domains(None, None, actor=actor)
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
      microformats2.prefix_image_urls(a, IMAGE_PROXY_URL_BASE)

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

  def check_token_for_actor(self, actor):
    """Checks that the given actor is public and matches the request's token.

    Raises: :class:`HTTPException` with HTTP 403
    """
    if not actor:
      error('Missing actor!')

    if not gr_source.Source.is_public(actor):
      error(f'Your {self.gr_source().NAME} account is private. Bridgy only supports public accounts.')

    token = request.values['token']
    domains = {util.domain_from_link(util.replace_test_domains_with_localhost(u))
               for u in microformats2.object_urls(actor)}
    domains.discard(self.source_class().GR_CLASS.DOMAIN)

    logging.info(f'Checking token against domains {domains}')
    for domain in ndb.get_multi(ndb.Key(Domain, d) for d in domains):
      if domain and token in domain.tokens:
        return

    error(f'Token {token} is not authorized for any of: {domains}', 403)

  def auth(self):
    """Loads the source and token and checks that they're valid.

    Expects token in the `token` query param, source in `key` or `username`.

    Raises: :class:`HTTPException` with HTTP 400 if the token or source are
      missing or invalid

    Returns: BrowserSource or None
    """
    # Load source
    source = util.load_source()

    # Load and check token
    token = request.values['token']
    for domain in Domain.query(Domain.tokens == token):
      if domain.key.id() in source.domains:
        return source

    error(f'Token {token} is not authorized for any of: {source.domains}', 403)


class Status(BrowserView):
  """Runs preflight checks for a source and returns status and config info.

  Response body is a JSON map with these fields:
    status: string, 'enabled' or 'disabled'
    poll-seconds: integer, current poll frequency for this source in seconds
  """
  def dispatch_request(self):
    source = self.auth()
    logging.info(f'Got source: {source}')

    out = {
      'status': source.status,
      'poll-seconds': source.poll_period().total_seconds(),
    }
    logging.info(f'Returning {out}')
    return out


class Homepage(BrowserView):
  """Parses a silo home page and returns the logged in user's username.

  Request body is https://www.instagram.com/ HTML for a logged in user.
  """
  def dispatch_request(self):
    gr_src = self.gr_source()
    _, actor = gr_src.scraped_to_activities(request.get_data(as_text=True))
    logging.info(f'Got actor: {actor}')

    if actor:
      username = actor.get('username')
      if username:
        logging.info(f'Returning {username}')
        return jsonify(username)

    error(f"Couldn't determine logged in {gr_src.NAME} user or username")


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
    activities, actor = self.gr_source().scraped_to_activities(
      request.get_data(as_text=True))
    ids = ' '.join(a['id'] for a in activities)
    logging.info(f"Returning activities: {ids}")
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
    self.check_token_for_actor(actor)

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
      error(f'No {gr_src.NAME} post found in HTML')

    @ndb.transactional()
    def update_activity():
      id = new_activity.get('id')
      if not id:
        error('Scraped post missing id')
      activity = Activity.get_by_id(id)

      if activity:
        # we already have this activity! merge in any new comments.
        merged_activity = copy.deepcopy(new_activity)
        existing_activity = json_loads(activity.activity_json)
        # TODO: extract out merging replies
        replies = merged_activity.setdefault('object', {}).setdefault('replies', {})
        gr_source.merge_by_id(replies, 'items',
          existing_activity.get('object', {}).get('replies', {}).get('items', []))
        replies['totalItems'] = len(replies.get('items', []))
        # TODO: merge tags too
        activity.activity_json = json_dumps(merged_activity)
      else:
        activity = Activity(id=id, source=source.key,
                            html=request.get_data(as_text=True),
                            activity_json=json_dumps(new_activity))

      # store and return the activity
      activity.put()
      logging.info(f"Stored activity {id}")

    update_activity()
    return new_activity


class Reactions(BrowserView):
  """Parses reactions/likes from silo HTML and adds them to an existing Activity.

  Requires the request parameter `id` with the silo post's id (not shortcode!).

  Response body is the translated ActivityStreams JSON for the reactions.
  """
  def dispatch_request(self, *args):
    source = self.auth()

    gr_src = self.gr_source()
    id = request.values['id']

    # validate request
    parsed_id = util.parse_tag_uri(id)
    if not parsed_id:
      error(f'Expected id to be tag URI; got {id}')

    activity = Activity.get_by_id(id)
    if not activity:
      error(f'No {gr_src.NAME} post found for id {id}', 404)
    elif activity.source != source.key:
      error(f'Activity {id} is owned by {activity.source}, not {source.key}', 403)

    activity_data = json_loads(activity.activity_json)

    # convert new reactions to AS, merge into existing activity
    try:
      new_reactions = gr_src.merge_scraped_reactions(
        request.get_data(as_text=True), activity_data)
    except ValueError as e:
      msg = f"Couldn't parse scraped reactions: {e}"
      logging.error(msg, exc_info=True)
      error(msg)

    activity.activity_json = json_dumps(activity_data)
    activity.put()

    reaction_ids = ' '.join(r['id'] for r in new_reactions)
    logging.info(f"Stored reactions for activity {id}: {reaction_ids}")
    return jsonify(new_reactions)


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
      error(f'No registered domains for token {token}', 404)

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
      (f'/{source_cls.SHORT_NAME}/browser/reactions', Reactions),
      (f'/{source_cls.SHORT_NAME}/browser/poll', Poll),
      (f'/{source_cls.SHORT_NAME}/browser/token-domains', TokenDomains),
    ):
    app.add_url_rule(route, view_func=cls.as_view(route),
                     methods=['GET', 'POST'] if cls == Status else ['POST'])
