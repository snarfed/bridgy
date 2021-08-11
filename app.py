"""Bridgy user-facing handlers: front page, user pages, and delete POSTs.
"""
import datetime
import importlib
import itertools
import logging
import string
import urllib.request, urllib.parse, urllib.error

from flask import Flask, redirect, render_template, request
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
from oauth_dropins.webutil import appengine_config, appengine_info
app = Flask('bridgy')
app.template_folder = './templates'
app.config.from_mapping(
    ENV='development' if appengine_info.DEBUG else 'PRODUCTION',
    CACHE_TYPE='SimpleCache',
    SECRET_KEY=util.read('flask_secret_key'),
    JSONIFY_PRETTYPRINT_REGULAR=True,
)
app.url_map.converters['regex'] = flask_util.RegexConverter
app.after_request(flask_util.default_modern_headers)
app.register_error_handler(Exception, flask_util.handle_exception)
app.before_request(flask_util.canonicalize_domain(
  util.OTHER_DOMAINS, util.PRIMARY_DOMAIN))

app.wsgi_app = flask_util.ndb_context_middleware(
    app.wsgi_app, client=appengine_config.ndb_client)

app.jinja_env.globals['naturaltime'] = humanize.naturaltime

cache = Cache(app)


class DashboardHandler(util.View):
  """Base handler for both the front page and user pages."""

  def head(self, *args, **kwargs):
    """Return an empty 200 with no caching directives."""

  def get(self, *args, **kwargs):
    return super().get(*args, **kwargs)

  def content_type(self):
    return 'text/html; charset=utf-8'

  def template_vars(self):
    return {
      'request': request,
      'logins': self.get_logins(),
      'DEBUG': appengine_info.DEBUG,
    }

  def headers(self):
    """Omit Cache-Control header."""
    headers = super().headers()
    headers.pop('Cache-Control', None)
    return headers


class CachedPageHandler(DashboardHandler):
  """Handle a page that may be cached with :class:`CachedPage`.

  Doesn't use the cache when:
  * running in dev_appserver
  * there are any query params
  * there's a logins cookie
  """

  EXPIRES = None  # subclasses can override

  def get(self, cache=True):
    if (not cache or appengine_info.LOCAL or request.params or
        self.get_logins()):
      return super().get()

    self.response.headers['Content-Type'] = self.content_type()
    cached = util.CachedPage.load(request.path)
    if cached:
      self.response.write(cached.html)
    else:
      super().get()
      util.CachedPage.store(request.path, self.response.body,
                            expires=self.EXPIRES)


class FrontPageHandler(CachedPageHandler):
  """Handler for the front page."""

  EXPIRES = datetime.timedelta(days=1)

  # Facebook uses a POST instead of a GET when it renders us in Canvas.
  # http://stackoverflow.com/a/5353413/186123
  post = CachedPageHandler.get

  def template_file(self):
    return 'index.html'

  def template_vars(self):
    """Use datastore stats to show stats for various things.

    https://developers.google.com/appengine/docs/python/ndb/admin#Statistics_queries
    """
    def count(query):
      stat = query.get()  # no datastore stats in dev_appserver
      return stat.count if stat else 0

    def kind_count(kind):
      return count(KindStat.query(KindStat.kind_name == kind))

    num_users = sum(kind_count(cls.__name__) for cls in models.sources.values())
    link_counts = {
      property: sum(count(KindPropertyNamePropertyTypeStat.query(
        KindPropertyNamePropertyTypeStat.kind_name == kind,
        KindPropertyNamePropertyTypeStat.property_name == property,
        # specify string because there are also ~2M Response entities with null
        # values for some of these properties, as opposed to missing altogether,
        # which we don't want to include.
        KindPropertyNamePropertyTypeStat.property_type == 'String'))
                    for kind in ('BlogPost', 'Response'))
      for property in ('sent', 'unsent', 'error', 'failed', 'skipped')}

    vars = super().template_vars()
    vars['sources'] = models.sources

    # stats; add comma separator between thousands
    vars.update({k: '{:,}'.format(v) for k, v in {
      'users': num_users,
      'responses': kind_count('Response'),
      'links': sum(link_counts.values()),
      'webmentions': link_counts['sent'] + kind_count('BlogPost'),
      'publishes': kind_count('Publish'),
      'blogposts': kind_count('BlogPost'),
      'webmentions_received': kind_count('BlogWebmention'),
    }.items()})
    return vars


class UsersHandler(CachedPageHandler):
  """Handler for /users.

  Semi-optimized. Pages by source name. Queries each source type for results
  with name greater than the start_name query param, then merge sorts the
  results and truncates at PAGE_SIZE.

  The start_name param is expected to be capitalized because capital letters
  sort lexicographically before lower case letters. An alternative would be to
  store a lower cased version of the name in another property and query on that.
  """
  PAGE_SIZE = 50

  def get(self):
    # only cache the first page
    return super().get(cache=not request.params)

  def template_file(self):
    return 'users.html'

  def template_vars(self):
    start_name = request.values.get('start_name')
    queries = [cls.query(cls.name >= start_name).fetch_async(self.PAGE_SIZE)
               for cls in models.sources.values()]

    sources = sorted(itertools.chain(*[q.get_result() for q in queries]),
                     key=lambda s: (s.name.lower(), s.GR_CLASS.NAME))
    sources = [self.preprocess_source(s) for s in sources
               if s.name.lower() >= start_name.lower() and s.features
                  and s.status != 'disabled'
               ][:self.PAGE_SIZE]

    vars = super().template_vars()
    vars.update({
        'sources': sources,
        'string': string,  # module, used in template
        'PAGE_SIZE': self.PAGE_SIZE,
        })
    return vars


class UserHandler(DashboardHandler):
  """Handler for a user page."""

  def get(self, source_short_name, id):
    cls = models.sources[source_short_name]
    self.source = cls.lookup(id)

    if not self.source:
      key = cls.query(ndb.OR(*[ndb.GenericProperty(prop) == id for prop in
                               ('domains', 'inferred_username', 'name', 'username')])
                      ).get(keys_only=True)
      if key:
        return redirect(cls(key=key).bridgy_path(), permanent=True)

    if self.source and self.source.features:
      self.source.verify()
      self.source = self.preprocess_source(self.source)
    else:
      self.response.status_int = 404
    super().get()

  def template_file(self):
    return ('%s_user.html' % self.source.SHORT_NAME
            if self.source and self.source.features
            else 'user_not_found.html')

  def headers(self):
    """Override the default and omit Cache-Control."""
    return {'Access-Control-Allow-Origin': '*'}

  def template_vars(self):
    vars = super().template_vars()
    vars.update({
      'source': self.source,
      'sources': models.sources,
      'logs': logs,
      'EPOCH': util.EPOCH,
      'REFETCH_HFEED_TRIGGER': models.REFETCH_HFEED_TRIGGER,
      'RECENT_PRIVATE_POSTS_THRESHOLD': RECENT_PRIVATE_POSTS_THRESHOLD,
    })
    if not self.source:
      return vars

    # Blog webmention promos
    if 'webmention' not in self.source.features:
      if self.source.SHORT_NAME in ('blogger', 'medium', 'tumblr', 'wordpress'):
        vars[self.source.SHORT_NAME + '_promo'] = True
      else:
        for domain in self.source.domains:
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
    if 'listen' in self.source.features or 'email' in self.source.features:
      vars['responses'] = []
      query = Response.query().filter(Response.source == self.source.key)

      # if there's a paging param (responses_before or responses_after), update
      # query with it
      def get_paging_param(param):
        val = request.values.get(param)
        try:
          return util.parse_iso8601(val) if val else None
        except:
          msg = "Couldn't parse %s %r as ISO8601" % (param, val)
          logging.warning(msg, stack_info=True)
          abort(400, msg)

      before = get_paging_param('responses_before')
      after = get_paging_param('responses_after')
      if before and after:
        abort(400, "can't handle both responses_before and responses_after")
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

        if (not self.source.is_activity_public(r.response) or
            not all(self.source.is_activity_public(a) for a in r.activities)):
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
          r.actor['image']['url'] = util.update_scheme(image_url, self)

        # generate original post links
        r.links = self.process_webmention_links(r)
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
        self.source.last_poll_attempt + self.source.poll_period(),
        # lower bound is 1 minute from now
        util.now_fn() + datetime.timedelta(seconds=90))

    # Publishes
    if 'publish' in self.source.features:
      publishes = Publish.query().filter(Publish.source == self.source.key)\
                                 .order(-Publish.updated)\
                                 .fetch(10)
      for p in publishes:
        p.pretty_page = util.pretty_link(
          p.key.parent().id(),
          attrs={'class': 'original-post u-url u-name'},
          new_tab=True)

      vars['publishes'] = publishes

    if 'webmention' in self.source.features:
      # Blog posts
      blogposts = BlogPost.query().filter(BlogPost.source == self.source.key)\
                                  .order(-BlogPost.created)\
                                  .fetch(10)
      for b in blogposts:
        b.links = self.process_webmention_links(b)
        try:
          text = b.feed_item.get('title')
        except ValueError:
          text = None
        b.pretty_url = util.pretty_link(
          b.key.id(), text=text, attrs={'class': 'original-post u-url u-name'},
          max_length=40, new_tab=True)

      # Blog webmentions
      webmentions = BlogWebmention.query()\
          .filter(BlogWebmention.source == self.source.key)\
          .order(-BlogWebmention.updated)\
          .fetch(10)
      for w in webmentions:
        w.pretty_source = util.pretty_link(
          w.source_url(), attrs={'class': 'original-post'}, new_tab=True)
        try:
          target_is_source = (urllib.parse.urlparse(w.target_url()).netloc in
                              self.source.domains)
        except BaseException:
          target_is_source = False
        w.pretty_target = util.pretty_link(
          w.target_url(), attrs={'class': 'original-post'}, new_tab=True,
          keep_host=target_is_source)

      vars.update({'blogposts': blogposts, 'webmentions': webmentions})

    return vars

  def process_webmention_links(self, e):
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


class AboutHandler(DashboardHandler):
  def template_file(self):
    return 'about.html'

  # when you click on a facebook notification, it POSTs, so accept POSTs too.
  post = DashboardHandler.get


class DeleteStart(util.View):
  def post(self):
    source = self.load_source(param='key')
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
            else '/%s/delete/finish' % source.SHORT_NAME)
    kwargs = {}
    if kind == 'Twitter':
      kwargs['access_type'] = 'read' if feature == 'listen' else 'write'

    handler = source.OAUTH_START.to(path, **kwargs)(request, self.response)
    try:
      return redirect(handler.redirect_url(state=state))
    except Exception as e:
      code, body = util.interpret_http_exception(e)
      if not code and util.is_connection_failure(e):
        code = '-'
        body = str(e)
      if code:
        self.messages.add('%s API error %s: %s' % (source.GR_CLASS.NAME, code, body))
        return redirect(source.bridgy_url(self))
      else:
        raise


class DeleteFinishHandler(util.View):
  def get(self):
    parts = util.decode_oauth_state(request.values.get('state') or '')
    callback = parts and parts.get('callback')

    if request.values.get('declined'):
      # disable declined means no change took place
      if callback:
        callback = util.add_query_params(callback, {'result': 'declined'})
        return redirect(callback)
      else:
        self.messages.add('If you want to disable, please approve the prompt.')
        return redirect('/')
      return

    if (not parts or 'feature' not in parts or 'source' not in parts):
      abort(400, 'state query parameter must include "feature" and "source"')

    feature = parts['feature']
    if feature not in (Source.FEATURES):
      abort(400, 'cannot delete unknown feature %s' % feature)

    logged_in_as = ndb.Key(
      urlsafe=flask_util.get_required_param('auth_entity')).get()
    source = ndb.Key(urlsafe=parts['source']).get()

    if logged_in_as and logged_in_as.is_authority_for(source.auth_entity):
      # TODO: remove credentials
      if feature in source.features:
        source.features.remove(feature)
        source.put()

        # remove login cookie
        logins = self.get_logins()
        login = util.Login(path=source.bridgy_path(), site=source.SHORT_NAME,
                           name=source.label_name())
        if login in logins:
          logins.remove(login)
          self.set_logins(logins)

      noun = 'webmentions' if feature == 'webmention' else feature + 'ing'
      if callback:
        callback = util.add_query_params(callback, {
          'result': 'success',
          'user': source.bridgy_url(self),
          'key': source.key.urlsafe().decode(),
        })
      else:
        self.messages.add('Disabled %s for %s. Sorry to see you go!' %
                          (noun, source.label()))
      # util.email_me(subject='Deleted Bridgy %s user: %s %s' %
      #               (feature, source.label(), source.key_id()),
      #               body=source.bridgy_url(self))
    else:
      if callback:
        callback = util.add_query_params(callback, {'result': 'failure'})
      else:
        self.messages.add('Please log into %s as %s to disable it here.' %
                          (source.GR_CLASS.NAME, source.name))

    return redirect(callback if callback
                  else source.bridgy_url(self) if source.features
                  else '/')


class PollNowHandler(util.View):
  source = None

  def post(self):
    self.get_source()
    util.add_poll_task(self.source, now=True)
    self.messages.add("Polling now. Refresh in a minute to see what's new!")
    return redirect(self.source.bridgy_url(self))

  def get_source(self):
    if not self.source:
      self.source = self.load_source(param='key')
    return self.source


class CrawlNowHandler(PollNowHandler):
  def post(self):
    self.setup_refetch_hfeed()
    util.add_poll_task(self.source, now=True)
    self.messages.add("Crawling now. Refresh in a minute to see what's new!")
    return redirect(self.source.bridgy_url(self))

  @ndb.transactional()
  def setup_refetch_hfeed(self):
    self.get_source()
    self.source.last_hfeed_refetch = models.REFETCH_HFEED_TRIGGER
    self.source.last_feed_syndication_url = None
    self.source.put()


class RetryHandler(util.View):
  def post(self):
    entity = self.load_source(param='key')
    if not isinstance(entity, Webmentions):
      abort(400, 'Unexpected key kind %s', entity.key.kind())

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
    self.messages.add('Retrying. Refresh in a minute to see the results!')
    return redirect(request.values.get('redirect_to') or
                  entity.source.get().bridgy_url(self))


class DiscoverHandler(util.View):
  def post(self):
    source = self.load_source()

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

    self.messages.add(msg)
    return redirect(source.bridgy_url(self))


class EditWebsites(util.View):

  def template_file(self):
    return 'edit_websites.html'

  def content_type(self):
    return 'text/html; charset=utf-8'

  def post(self):
    source = self.load_source()
    redirect_url = '%s?%s' % (request.path, urllib.parse.urlencode({
      'source_key': source.key.urlsafe().decode(),
    }))

    add = request.values.get('add')
    delete = request.values.get('delete')
    if (add and delete) or (not add and not delete):
      abort(400, 'Either add or delete param (but not both) required')

    link = util.pretty_link(add or delete)

    if add:
      resolved = Source.resolve_profile_url(add)
      if resolved:
        if resolved in source.domain_urls:
          self.messages.add('%s already exists.' % link)
        else:
          source.domain_urls.append(resolved)
          domain = util.domain_from_link(resolved)
          source.domains.append(domain)
          source.put()
          self.messages.add('Added %s.' % link)
      else:
        self.messages.add("%s doesn't look like your web site. Try again?" % link)

    else:
      assert delete
      try:
        source.domain_urls.remove(delete)
      except ValueError:
        abort(400, "%s not found in %s's current web sites" % (
                          delete, source.label()))
      domain = util.domain_from_link(delete)
      if domain not in set(util.domain_from_link(url) for url in source.domain_urls):
        source.domains.remove(domain)
      source.put()
      self.messages.add('Removed %s.' % link)

    return redirect(redirect_url)

  def template_vars(self):
    return {
      'source': self.preprocess_source(self.load_source()),
      'util': util,
    }


@app.route('/<any(listen, publish):_>', methods=('GET', 'HEAD'))
def redirect_to_front_page(_):
  """Redirect to the front page."""
  return redirect(util.add_query_params('/', request.values.items()), code=301)


@app.route('/logout')
def logout():
  """Redirect to the front page."""
  self.set_logins([])
  self.messages.add('Logged out.')
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


# routes += [
#   ('/?', FrontPage),
#   ('/users/?', Users),
#   ('/(blogger|fake|fake_blog|flickr|github|instagram|mastodon|medium|meetup|reddit|tumblr|twitter|wordpress)/([^/]+)/?',
#    User),
#   ('/about/?', About),
#   ('/delete/start', DeleteStart),
#   ('/delete/finish', DeleteFinish),
#   ('/discover', Discover),
#   ('/poll-now', PollNow),
#   ('/crawl-now', CrawlNow),
#   ('/retry', Retry),
#   ('/edit-websites', EditWebsites),
# ]
