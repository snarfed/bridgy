"""Bridgy user-facing views: front page, user pages, and delete POSTs.
"""
import datetime
import importlib
import itertools
import logging
import string
import urllib.request, urllib.parse, urllib.error

from flask import flash, Flask, redirect, render_template, request
from flask_caching import Cache
from google.cloud import ndb
from google.cloud.ndb.stats import KindStat, KindPropertyNamePropertyTypeStat
import humanize
from oauth_dropins import indieauth
from oauth_dropins.webutil import appengine_info, flask_util, logs
from oauth_dropins.webutil.appengine_config import ndb_client
from oauth_dropins.webutil.util import json_dumps, json_loads
import webapp2

import appengine_config
from blogger import Blogger
from tumblr import Tumblr
from wordpress_rest import WordPress
import models
from models import BlogPost, BlogWebmention, Publish, Response, Source, Webmentions
import original_post_discovery
import util

RECENT_PRIVATE_POSTS_THRESHOLD = 5


# Flask app
app = Flask('bridgy')
app.template_folder = './templates'
app.config.from_pyfile('config.py')
app.url_map.converters['regex'] = flask_util.RegexConverter
app.after_request(flask_util.default_modern_headers)
app.register_error_handler(Exception, flask_util.handle_exception)
app.before_request(flask_util.canonicalize_domain(
  util.OTHER_DOMAINS, util.PRIMARY_DOMAIN))

app.wsgi_app = flask_util.ndb_context_middleware(app.wsgi_app, client=ndb_client)

app.jinja_env.globals.update({
  'naturaltime': humanize.naturaltime,
  'get_logins': util.get_logins,
  'sources': models.sources,
  'string': string,
  'util': util,
})

cache = Cache(app)

# Import source class files so their metaclasses are initialized.
import blogger, facebook, flickr, github, instagram, mastodon, meetup, medium, reddit, tumblr, twitter, wordpress_rest

@app.route('/', methods=['HEAD'])
@app.route('/users', methods=['HEAD'])
@app.route('/<site>/<id>', methods=['HEAD'])
@app.route('/about', methods=['HEAD'])
def head(site=None, id=None):
  """Return an empty 200 with no caching directives."""
  if site and site not in models.sources:
    return '', 404

  return ''


@app.route('/')
@cache.cached(datetime.timedelta(days=1).total_seconds())
def front_page():
  """View for the front page."""
  return render_template('index.html')


@app.route('/about')
def about():
  return render_template('about.html')


@app.route('/users')
@cache.cached(datetime.timedelta(hours=1).total_seconds(),
              unless=lambda: request.query_string)
def users():
  """View for /users.

  Semi-optimized. Pages by source name. Queries each source type for results
  with name greater than the start_name query param, then merge sorts the
  results and truncates at PAGE_SIZE.

  The start_name param is expected to be capitalized because capital letters
  sort lexicographically before lower case letters. An alternative would be to
  store a lower cased version of the name in another property and query on that.
  """
  PAGE_SIZE = 50

  start_name = request.values.get('start_name')
  queries = [cls.query(cls.name >= start_name).fetch_async(PAGE_SIZE)
             for cls in models.sources.values()]

  sources = sorted(itertools.chain(*[q.get_result() for q in queries]),
                   key=lambda s: (s.name.lower(), s.GR_CLASS.NAME))
  sources = [util.preprocess_source(s) for s in sources
             if s.name.lower() >= start_name.lower() and s.features
                and s.status != 'disabled'
             ][:PAGE_SIZE]

  return render_template('users.html', PAGE_SIZE=PAGE_SIZE)


@app.route('/<site>/<id>')
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
    'EPOCH': util.EPOCH,
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
        return util.parse_iso8601(val) if val else None
      except:
        error("Couldn't parse %s %r as ISO8601" % (param, val))

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

      if (not source.is_activity_public(r.response) or
          not all(source.is_activity_public(a) for a in r.activities)):
        continue
      elif r.type == 'post':
        r.activities = []

      verb = r.response.get('verb')
      r.actor = (r.response.get('object') if verb == 'invite'
                 else r.response.get('author') or r.response.get('actor')
                ) or {}

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
        r.response['content'] = '%s %s.' % (
          r.actor.get('displayName') or '', phrase)

      # convert image URL to https if we're serving over SSL
      image_url = r.actor.setdefault('image', {}).get('url')
      if image_url:
        r.actor['image']['url'] = util.update_scheme(image_url, request)

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
      vars['responses_after_link'] = ('?responses_after=%s#responses' %
                                       new_after.isoformat())

    new_before = (
      after if after else
      vars['responses'][-1].updated if
        vars['responses'] and query_iter.probably_has_next()
      else None)
    if new_before:
      vars['responses_before_link'] = ('?responses_before=%s#responses' %
                                       new_before.isoformat())

    vars['next_poll'] = max(
      source.last_poll_attempt + source.poll_period(),
      # lower bound is 1 minute from now
      util.now_fn() + datetime.timedelta(seconds=90))

  # Publishes
  if 'publish' in source.features:
    publishes = Publish.query().filter(Publish.source == source.key)\
                               .order(-Publish.updated)\
                               .fetch(10)
    for p in publishes:
      p.pretty_page = util.pretty_link(
        p.key.parent().id(),
        attrs={'class': 'original-post u-url u-name'},
        new_tab=True)

    vars['publishes'] = publishes

  if 'webmention' in source.features:
    # Blog posts
    blogposts = BlogPost.query().filter(BlogPost.source == source.key)\
                                .order(-BlogPost.created)\
                                .fetch(10)
    for b in blogposts:
      b.links = process_webmention_links(b)
      try:
        text = b.feed_item.get('title')
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
  """Generates pretty HTML for the links in a :class:`Webmentions` entity.

  Args:
    e: :class:`Webmentions` subclass (:class:`Response` or :class:`BlogPost`)
  """
  link = lambda url, g: util.pretty_link(
    url, glyphicon=g, attrs={'class': 'original-post u-bridgy-target'}, new_tab=True)
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
  feature = flask_util.get_required_param('feature')
  state = util.encode_oauth_state({
    'operation': 'delete',
    'feature': feature,
    'source': source.key.urlsafe().decode(),
    'callback': request.values.get('callback'),
  })

  # Blogger don't support redirect_url() yet
  if kind == 'Blogger':
    return redirect('/blogger/delete/start?state=%s' % state)

  path = ('/reddit/callback' if kind == 'Reddit'
          else '/wordpress/add' if kind == 'WordPress'
          else f'/{source.SHORT_NAME}/delete/finish')
  kwargs = {}
  if kind == 'Twitter':
    kwargs['access_type'] = 'read' if feature == 'listen' else 'write'

  try:
    return redirect(source.OAUTH_START(path).redirect_url(state=state))
  except Exception as e:
    code, body = util.interpret_http_exception(e)
    if not code and util.is_connection_failure(e):
      code = '-'
      body = str(e)
    if code:
      flash('%s API error %s: %s' % (source.GR_CLASS.NAME, code, body))
      return redirect(source.bridgy_url(self))
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

  feature = parts['feature']
  if feature not in (Source.FEATURES):
    error('cannot delete unknown feature %s' % feature)

  logged_in_as = ndb.Key(
    urlsafe=flask_util.get_required_param('auth_entity')).get()
  source = ndb.Key(urlsafe=parts['source']).get()

  if logged_in_as and logged_in_as.is_authority_for(source.auth_entity):
    # TODO: remove credentials
    if feature in source.features:
      source.features.remove(feature)
      source.put()

      # remove login cookie
      logins = util.get_logins()
      login = util.Login(path=source.bridgy_path(), site=source.SHORT_NAME,
                         name=source.label_name())
      if login in logins:
        logins.remove(login)
        util.set_logins(logins)

    noun = 'webmentions' if feature == 'webmention' else feature + 'ing'
    if callback:
      callback = util.add_query_params(callback, {
        'result': 'success',
        'user': source.bridgy_url(),
        'key': source.key.urlsafe().decode(),
      })
    else:
      flash(f'Disabled {noun} for {source.label()}. Sorry to see you go!')
  else:
    if callback:
      callback = util.add_query_params(callback, {'result': 'failure'})
    else:
      flash(f'Please log into {source.GR_CLASS.NAME} as {source.name} to disable it here.')

  return redirect(callback if callback
                  else source.bridgy_url() if source.features
                  else '/')


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
  url = flask_util.get_required_param('url')
  domain = util.domain_from_link(url)
  path = urllib.parse.urlparse(url).path
  msg = 'Discovering now. Refresh in a minute to see the results!'

  gr_source = source.gr_source
  if domain == gr_source.DOMAIN:
    post_id = gr_source.post_id(url)
    if post_id:
      type = 'event' if path.startswith('/events/') else None
      util.add_discover_task(source, post_id, type=type)
    else:
      msg = "Sorry, that doesn't look like a %s post URL." % gr_source.NAME

  elif util.domain_or_parent_in(domain, source.domains):
    synd_links = original_post_discovery.process_entry(source, url, {}, False, [])
    if synd_links:
      for link in synd_links:
        util.add_discover_task(source, gr_source.post_id(link))
      source.updates = {'last_syndication_url': util.now_fn()}
      models.Source.put_updates(source)
    else:
      msg = 'Failed to fetch %s or find a %s syndication link.' % (
        util.pretty_link(url), gr_source.NAME)

  else:
    msg = 'Please enter a URL on either your web site or %s.' % gr_source.NAME

  flash(msg)
  return redirect(source.bridgy_url())


@app.route('/edit-websites', methods=['GET'])
def edit_websites_get():
  return render_template('edit_websites.html',
                         source=util.preprocess_source(util.load_source()))


@app.route('/edit-websites', methods=['POST'])
def edit_websites_post():
  source = util.load_source()
  redirect_url = '%s?%s' % (request.path, urllib.parse.urlencode({
    'source_key': source.key.urlsafe().decode(),
  }))

  add = request.values.get('add')
  delete = request.values.get('delete')
  if (add and delete) or (not add and not delete):
    error('Either add or delete param (but not both) required')

  link = util.pretty_link(add or delete)

  if add:
    resolved = Source.resolve_profile_url(add)
    if resolved:
      if resolved in source.domain_urls:
        flash('%s already exists.' % link)
      else:
        source.domain_urls.append(resolved)
        domain = util.domain_from_link(resolved)
        source.domains.append(domain)
        source.put()
        flash('Added %s.' % link)
    else:
      flash("%s doesn't look like your web site. Try again?" % link)

  else:
    assert delete
    try:
      source.domain_urls.remove(delete)
    except ValueError:
      error(f"{delete} not found in {source.label()}'s current web sites")
    domain = util.domain_from_link(delete)
    if domain not in set(util.domain_from_link(url) for url in source.domain_urls):
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
  util.set_logins([])
  flash('Logged out.')
  return redirect('/')


@app.route('/csp-report')
def csp_report():
  """Log Content-Security-Policy reports. https://content-security-policy.com/"""
  logging.info(request.values.get_data(as_text=True))
  return 'OK'


@app.route('/log')
@cache.cached(logs.CACHE_TIME.total_seconds())
def log():
    return logs.log()


@app.route('/_ah/<any(start, stop, warmup):_>')
def noop(_):
  return 'OK'


import handlers
