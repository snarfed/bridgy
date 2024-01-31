"""Bridgy user-facing pages: front page, user pages, delete POSTs, etc."""
import datetime
import itertools
import logging
import urllib.request, urllib.parse, urllib.error

from flask import render_template, request
from google.cloud import ndb
from granary import as1
from granary.source import html_to_text
from oauth_dropins.webutil import logs
from oauth_dropins.webutil import flask_util
from oauth_dropins.webutil.flask_util import error, flash
from oauth_dropins.webutil.util import json_dumps, json_loads
import werkzeug.exceptions

from flask_app import app, cache
from blogger import Blogger
import models
from models import BlogPost, BlogWebmention, Publish, Response, Source, Webmentions
import original_post_discovery
from tumblr import Tumblr
import util
from util import redirect
from wordpress_rest import WordPress

# populate models.sources
import blogger, bluesky, facebook, flickr, github, indieauth, instagram, mastodon, medium, reddit, tumblr, twitter, wordpress_rest

logger = logging.getLogger(__name__)

SITES = ','.join(list(models.sources.keys()) + ['fake'])  # for unit tests

RECENT_PRIVATE_POSTS_THRESHOLD = 5


@app.route('/', methods=['HEAD'])
@app.route('/users', methods=['HEAD'])
@app.route(f'/<any({SITES}):site>/<id>', methods=['HEAD'])
@app.route('/about', methods=['HEAD'])
def head(site=None, id=None):
  """Return an empty 200 with no caching directives."""
  if site and site not in models.sources:
    return '', 404

  return ''


@app.route('/')
@flask_util.cached(cache, datetime.timedelta(days=1))
def front_page():
  """View for the front page."""
  return render_template('index.html')


@app.route('/about')
def about():
  return render_template('about.html')


@app.route('/which-bridgy')
def which_bridgy():
  return render_template('mastodon_which_bridgy.html')


@app.route('/users')
@flask_util.cached(cache, datetime.timedelta(hours=1))
def users():
  """View for ``/users``.

  Semi-optimized. Pages by source name. Queries each source type for results
  with name greater than the start_name query param, then merge sorts the
  results and truncates at ``PAGE_SIZE``\.

  The start_name param is expected to be capitalized because capital letters
  sort lexicographically before lower case letters. An alternative would be to
  store a lower cased version of the name in another property and query on that.
  """
  PAGE_SIZE = 50

  start_name = request.values.get('start_name', '')
  queries = [cls.query(cls.name >= start_name).fetch_async(PAGE_SIZE)
             for cls in models.sources.values()]

  sources = sorted(itertools.chain(*[q.get_result() for q in queries]),
                   key=lambda s: (s.name.lower(), s.GR_CLASS.NAME))
  sources = [util.preprocess_source(s) for s in sources
             if s.name.lower() >= start_name.lower() and s.features
                and s.status != 'disabled'
             ][:PAGE_SIZE]

  return render_template('users.html', PAGE_SIZE=PAGE_SIZE, sources=sources)


@app.route(f'/<any({SITES}):site>/<id>')
def user(site, id):
  """View for a user page."""
  cls = models.sources.get(site)
  if not cls:
    return render_template('user_not_found.html'), 404

  source = cls.lookup(id)

  if not source:
    key = cls.query(ndb.OR(*[ndb.GenericProperty(prop) == id for prop in
                             ('domains', 'inferred_username', 'name', 'username')])
                    ).get(keys_only=True)
    if key:
      return redirect(cls(key=key).bridgy_path(), code=301)

  if not source or not source.features:
    return render_template('user_not_found.html'), 404

  source.verify()
  source = util.preprocess_source(source)

  vars = {
    'source': source,
    'logs': logs,
    'REFETCH_HFEED_TRIGGER': models.REFETCH_HFEED_TRIGGER,
    'RECENT_PRIVATE_POSTS_THRESHOLD': RECENT_PRIVATE_POSTS_THRESHOLD,
  }

  # Blog webmention promos
  if 'webmention' not in source.features:
    if source.SHORT_NAME in ('blogger', 'medium', 'tumblr', 'wordpress'):
      vars[source.SHORT_NAME + '_promo'] = True
    else:
      for domain in source.domains:
        if ('.blogspot.' in domain and  # Blogger uses country TLDs
            not Blogger.query(Blogger.domains == domain).get()):
          vars['blogger_promo'] = True
        elif (util.domain_or_parent_in(domain, ['tumblr.com']) and
              not Tumblr.query(Tumblr.domains == domain).get()):
          vars['tumblr_promo'] = True
        elif (util.domain_or_parent_in(domain, 'wordpress.com') and
              not WordPress.query(WordPress.domains == domain).get()):
          vars['wordpress_promo'] = True

  # Responses
  if 'listen' in source.features or 'email' in source.features:
    vars['responses'] = []
    query = Response.query().filter(Response.source == source.key)

    # if there's a paging param (responses_before or responses_after), update
    # query with it
    def get_paging_param(param):
      val = request.values.get(param)
      try:
        return util.parse_iso8601(val.replace(' ', '+')) if val else None
      except BaseException:
        error(f"Couldn't parse {param}, {val!r} as ISO8601")

    before = get_paging_param('responses_before')
    after = get_paging_param('responses_after')
    if before and after:
      error("can't handle both responses_before and responses_after")
    elif after:
      query = query.filter(Response.updated > after).order(Response.updated)
    elif before:
      query = query.filter(Response.updated < before).order(-Response.updated)
    else:
      query = query.order(-Response.updated)

    query_iter = query.iter()
    for i, r in enumerate(query_iter):
      r.response = json_loads(r.response_json)
      r.activities = [json_loads(a) for a in r.activities_json]

      if (not as1.is_public(r.response) or
          not all(as1.is_public(a) for a in r.activities)):
        continue
      elif r.type == 'post':
        r.activities = []

      verb = r.response.get('verb')
      r.actor = (r.response.get('object') if verb == 'invite'
                 else r.response.get('author') or r.response.get('actor')
                ) or {}
      r.actor['url'] = as1.get_url(r.actor)

      activity_content = ''
      for a in r.activities + [r.response]:
        if not a.get('content'):
          obj = a.get('object', {})
          a['content'] = activity_content = (
            obj.get('content') or obj.get('displayName') or
            # historical, from a Reddit bug fixed in granary@4f9df7c
            obj.get('name') or '')

      response_content = r.response.get('content')
      phrases = {
        'like': 'liked this',
        'repost': 'reposted this',
        'rsvp-yes': 'is attending',
        'rsvp-no': 'is not attending',
        'rsvp-maybe': 'might attend',
        'rsvp-interested': 'is interested',
        'invite': 'is invited',
      }
      phrase = phrases.get(r.type) or phrases.get(verb)
      if phrase and (r.type != 'repost' or
                     activity_content.startswith(response_content)):
        r.response['content'] = f'{r.actor.get("displayName") or ""} {phrase}.'

      # convert image URL to https if we're serving over SSL
      # account for fact image might be a list
      image = util.get_first(r.actor, 'image', {})
      image_url = image.get('url')
      if image_url:
        image['url'] = util.update_scheme(image_url, request)
      r.actor['image'] = image
      # generate original post links
      r.links = process_webmention_links(r)
      r.original_links = [util.pretty_link(url, new_tab=True)
                          for url in r.original_posts]

      vars['responses'].append(r)
      if len(vars['responses']) >= 10 or i > 200:
        break

    vars['responses'].sort(key=lambda r: r.updated, reverse=True)

    # calculate new paging param(s)
    new_after = (
      before if before else
      vars['responses'][0].updated if
        vars['responses'] and query_iter.probably_has_next() and (before or after)
      else None)
    if new_after:
      vars['responses_after_link'] = f'?responses_after={new_after.isoformat()}#responses'

    new_before = (
      after if after else
      vars['responses'][-1].updated if
        vars['responses'] and query_iter.probably_has_next()
      else None)
    if new_before:
      vars['responses_before_link'] = f'?responses_before={new_before.isoformat()}#responses'

    vars['next_poll'] = max(source.last_poll_attempt + source.poll_period(),
                            # lower bound is 1 minute from now
                            util.now() + datetime.timedelta(seconds=90))

  # Publishes
  if 'publish' in source.features:
    publishes = Publish.query().filter(Publish.source == source.key)\
                               .order(-Publish.updated)\
                               .fetch(10)
    for p in publishes:
      parent = p.key.parent()
      published = p.published if isinstance(p.published, dict) else {}
      url = parent.id() if parent else published.get('url')
      text = published.get('text')

      if url:
        p.pretty_page = util.pretty_link(
          url,
          text=text,
          attrs={'class': 'original-post u-url u-name'},
          new_tab=True)
      else:
        p.pretty_page = text or ''

    vars['publishes'] = publishes

  if 'webmention' in source.features:
    # Blog posts
    blogposts = BlogPost.query().filter(BlogPost.source == source.key)\
                                .order(-BlogPost.created)\
                                .fetch(10)
    for b in blogposts:
      b.links = process_webmention_links(b)
      try:
        text = html_to_text(b.feed_item.get('title'))
      except ValueError:
        text = None
      b.pretty_url = util.pretty_link(
        b.key.id(), text=text, attrs={'class': 'original-post u-url u-name'},
        max_length=40, new_tab=True)

    # Blog webmentions
    webmentions = BlogWebmention.query()\
        .filter(BlogWebmention.source == source.key)\
        .order(-BlogWebmention.updated)\
        .fetch(10)
    for w in webmentions:
      w.pretty_source = util.pretty_link(
        w.source_url(), attrs={'class': 'original-post'}, new_tab=True)
      try:
        target_is_source = (urllib.parse.urlparse(w.target_url()).netloc in
                            source.domains)
      except BaseException:
        target_is_source = False
      w.pretty_target = util.pretty_link(
        w.target_url(), attrs={'class': 'original-post'}, new_tab=True,
        keep_host=target_is_source)

    vars.update({'blogposts': blogposts, 'webmentions': webmentions})

  return render_template(f'{source.SHORT_NAME}_user.html', **vars)


def process_webmention_links(e):
  """Generates pretty HTML for the links in a :class:`models.Webmentions` entity.

  Args:
    e (models.Response or models.BlogPost)
  """
  def link(url, g):
    return util.pretty_link(
      url, glyphicon=g, attrs={'class': 'original-post u-bridgy-target'},
      new_tab=True)

  return util.trim_nulls({
      'Failed': set(link(url, 'exclamation-sign') for url in e.error + e.failed),
      'Sending': set(link(url, 'transfer') for url in e.unsent
                     if url not in e.error),
      'Sent': set(link(url, None) for url in e.sent
                  if url not in (e.error + e.unsent)),
      'No <a href="http://indiewebify.me/#send-webmentions">webmention</a> '
      'support': set(link(url, None) for url in e.skipped),
      })


@app.route('/delete/start', methods=['POST'])
def delete_start():
  source = util.load_source()
  kind = source.key.kind()
  feature = request.form['feature']
  state = util.encode_oauth_state({
    'operation': 'delete',
    'feature': feature,
    'source': source.key.urlsafe().decode(),
    'callback': request.values.get('callback'),
  })

  if kind == 'Blogger':
    # Blogger doesn't support redirect_url() yet
    return redirect(f'/blogger/delete/start?state={state}')
  elif kind == 'Bluesky':
    # Bluesky isn't OAuth at all yet
    return redirect(f'/bluesky/delete/start')

  path = ('/reddit/callback' if kind == 'Reddit'
          else '/wordpress/add' if kind == 'WordPress'
          else f'/{source.SHORT_NAME}/delete/finish')

  try:
    return redirect(source.OAUTH_START(path).redirect_url(state=state))
  except ValueError as e:
      flash(f'Error: {e}')
      return redirect(source.bridgy_url())
  except werkzeug.exceptions.HTTPException:
    # raised by us, probably via self.error()
    raise
  except Exception as e:
    code, body = util.interpret_http_exception(e)
    if not code and util.is_connection_failure(e):
      code = '-'
      body = str(e)
    if code:
      flash(f'{source.GR_CLASS.NAME} API error {code}: {body}')
      return redirect(source.bridgy_url())
    else:
      raise


@app.route('/delete/finish')
def delete_finish():
  parts = util.decode_oauth_state(request.values.get('state') or '')
  callback = parts and parts.get('callback')

  if request.values.get('declined'):
    # disable declined means no change took place
    if callback:
      callback = util.add_query_params(callback, {'result': 'declined'})
      return redirect(callback)
    else:
      flash('If you want to disable, please approve the prompt.')
      return redirect('/')
    return

  if not parts or 'feature' not in parts or 'source' not in parts:
    error('state query parameter must include "feature" and "source"')

  features = parts['feature'].split(',')
  for feature in features:
    if feature not in (util.FEATURES):
      error(f'cannot delete unknown feature {feature}')

  logged_in_as = ndb.Key(urlsafe=request.args['auth_entity']).get()
  source = ndb.Key(urlsafe=parts['source']).get()

  logins = None
  if logged_in_as and logged_in_as.is_authority_for(source.auth_entity):
    source.features = set(source.features) - set(features)
    source.put()

    if not source.features:
      # remove login cookie
      logins = util.get_logins()
      login = util.Login(path=source.bridgy_path(), site=source.SHORT_NAME,
                         name=source.label_name())
      if login in logins:
        logins.remove(login)

    if callback:
      callback = util.add_query_params(callback, {
        'result': 'success',
        'user': source.bridgy_url(),
        'key': source.key.urlsafe().decode(),
      })
    else:
      nouns = {
        'webmention': 'webmentions',
        'listen': 'backfeed',
        'publish': 'publishing',
      }
      msg = f'Disabled {nouns[feature]} for {source.label()}.'
      if not source.features:
        msg += ' Sorry to see you go!'
      flash(msg)
  elif callback:
    callback = util.add_query_params(callback, {'result': 'failure'})
  else:
    flash(f'Please log into {source.GR_CLASS.NAME} as {source.name} to disable it here.')

  url = callback if callback else source.bridgy_url() if source.features else '/'
  return redirect(url, logins=logins)


@app.route('/poll-now', methods=['POST'])
def poll_now():
  source = util.load_source()
  util.add_poll_task(source, now=True)
  flash("Polling now. Refresh in a minute to see what's new!")
  return redirect(source.bridgy_url())


@app.route('/crawl-now', methods=['POST'])
def crawl_now():
  source = None

  @ndb.transactional()
  def setup_refetch_hfeed():
    nonlocal source
    source = util.load_source()
    source.last_hfeed_refetch = models.REFETCH_HFEED_TRIGGER
    source.last_feed_syndication_url = None
    source.put()

  setup_refetch_hfeed()
  util.add_poll_task(source, now=True)
  flash("Crawling now. Refresh in a minute to see what's new!")
  return redirect(source.bridgy_url())


@app.route('/retry', methods=['POST'])
def retry():
  entity = util.load_source()
  if not isinstance(entity, Webmentions):
    error(f'Unexpected key kind {entity.key.kind()}')

  source = entity.source.get()

  # run OPD to pick up any new SyndicatedPosts. note that we don't refetch
  # their h-feed, so if they've added a syndication URL since we last crawled,
  # retry won't make us pick it up. background in #524.
  if entity.key.kind() == 'Response':
    source = entity.source.get()
    for activity in [json_loads(a) for a in entity.activities_json]:
      originals, mentions = original_post_discovery.discover(
        source, activity, fetch_hfeed=False, include_redirect_sources=False)
      entity.unsent += original_post_discovery.targets_for_response(
        json_loads(entity.response_json), originals=originals, mentions=mentions)

  entity.restart()
  flash('Retrying. Refresh in a minute to see the results!')
  return redirect(request.values.get('redirect_to') or source.bridgy_url())


@app.route('/discover', methods=['POST'])
def discover():
  source = util.load_source()

  # validate URL, find silo post
  url = request.form['url']
  domain = util.domain_from_link(url)
  path = urllib.parse.urlparse(url).path
  msg = 'Discovering now. Refresh in a minute to see the results!'

  gr_source = source.gr_source
  if domain == gr_source.DOMAIN:
    post_id = source.post_id(url)
    if post_id:
      type = 'event' if path.startswith('/events/') else None
      util.add_discover_task(source, post_id, type=type)
    else:
      msg = f"Sorry, that doesn't look like a {gr_source.NAME} post URL."

  elif util.domain_or_parent_in(domain, source.domains):
    synd_links = original_post_discovery.process_entry(source, url, {}, False, [])
    if synd_links:
      for link in synd_links:
        util.add_discover_task(source, source.post_id(link))
      source.updates = {'last_syndication_url': util.now()}
      models.Source.put_updates(source)
    else:
      msg = f'Failed to fetch {util.pretty_link(url)} or find a {gr_source.NAME} syndication link.'

  else:
    msg = f'Please enter a URL on either your web site or {gr_source.NAME}.'

  flash(msg)
  return redirect(source.bridgy_url())


@app.route('/edit-websites', methods=['GET'])
def edit_websites_get():
  return render_template('edit_websites.html',
                         source=util.preprocess_source(util.load_source()))


@app.route('/edit-websites', methods=['POST'])
def edit_websites_post():
  source = util.load_source()
  redirect_url = f'{request.path}?{urllib.parse.urlencode({"source_key": source.key.urlsafe().decode()})}'

  add = request.values.get('add')
  delete = request.values.get('delete')
  if (add and delete) or (not add and not delete):
    error('Either add or delete param (but not both) required')

  link = util.pretty_link(add or delete)

  if add:
    resolved = Source.resolve_profile_url(add)
    if resolved:
      if resolved in source.domain_urls:
        flash(f'{link} already exists.')
      else:
        source.domain_urls.append(resolved)
        domain = util.domain_from_link(resolved)
        source.domains.append(domain)
        source.put()
        flash(f'Added {link}.')
    else:
      flash(f"{link} doesn't look like your web site. Try again?")

  else:
    assert delete
    try:
      source.domain_urls.remove(delete)
    except ValueError:
      error(f"{delete} not found in {source.label()}'s current web sites")
    domain = util.domain_from_link(delete)
    if domain not in {util.domain_from_link(url) for url in source.domain_urls}:
      source.domains.remove(domain)
    source.put()
    flash(f'Removed {link}.')

  return redirect(redirect_url)


@app.route('/<any(listen, publish):_>', methods=('GET', 'HEAD'))
def redirect_to_front_page(_):
  """Redirect to the front page."""
  return redirect(util.add_query_params('/', request.values.items()), code=301)


@app.route('/logout')
def logout():
  """Redirect to the front page."""
  flash('Logged out.')
  return redirect('/', logins=[])


@app.route('/log')
@flask_util.cached(cache, logs.CACHE_TIME)
def log():
    return logs.log(module=request.values.get('module'))
