"""Bridgy user-facing handlers: front page, user pages, and delete POSTs.
"""
from __future__ import absolute_import, unicode_literals

from future import standard_library
standard_library.install_aliases()
from builtins import str
import datetime
import itertools
import logging
import string
import urllib.request, urllib.parse, urllib.error

import appengine_config

from google.cloud import ndb
from google.cloud.ndb.stats import KindStat, KindPropertyNameStat
from oauth_dropins import (
  blogger as oauth_blogger,
  facebook as oauth_facebook,
  flickr as oauth_flickr,
  github as oauth_github,
  indieauth,
  instagram as oauth_instagram,
  medium as oauth_medium,
  mastodon as oauth_mastodon,
  tumblr as oauth_tumblr,
  twitter as oauth_twitter,
  wordpress_rest as oauth_wordpress_rest,
)
from oauth_dropins.webutil import handlers as webutil_handlers
from oauth_dropins.webutil.util import json_dumps, json_loads
import webapp2

from blogger import Blogger
from tumblr import Tumblr
from wordpress_rest import WordPress
import models
from models import BlogPost, BlogWebmention, Publish, Response, Source, Webmentions
import original_post_discovery
import util

# import source model class definitions for template rendering
import blogger, facebook, flickr, github, instagram, mastodon, medium, tumblr, twitter, wordpress_rest

RECENT_PRIVATE_POSTS_THRESHOLD = 5


class DashboardHandler(webutil_handlers.TemplateHandler, util.Handler):
  """Base handler for both the front page and user pages."""

  @util.canonicalize_domain
  def head(self, *args, **kwargs):
    """Return an empty 200 with no caching directives."""

  @util.canonicalize_domain
  def get(self, *args, **kwargs):
    return super(DashboardHandler, self).get(*args, **kwargs)

  def content_type(self):
    return 'text/html; charset=utf-8'

  def template_vars(self):
    return {
      'request': self.request,
      'logins': self.get_logins(),
      'DEBUG': appengine_config.DEBUG,
    }

  def headers(self):
    """Omit Cache-Control header."""
    headers = super(DashboardHandler, self).headers()
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

  @util.canonicalize_domain
  def get(self, cache=True):
    if (not cache or appengine_config.DEBUG or self.request.params or
        self.get_logins()):
      return super(CachedPageHandler, self).get()

    self.response.headers['Content-Type'] = self.content_type()
    cached = util.CachedPage.load(self.request.path)
    if cached:
      self.response.write(cached.html)
    else:
      super(CachedPageHandler, self).get()
      util.CachedPage.store(self.request.path, self.response.body,
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
      property: sum(count(KindPropertyNameStat.query(
          KindPropertyNameStat.kind_name == kind,
          KindPropertyNameStat.property_name == property))
                    for kind in ('BlogPost', 'Response'))
      for property in ('sent', 'unsent', 'error', 'failed', 'skipped')}

    vars = super(FrontPageHandler, self).template_vars()
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
  PAGE_SIZE = 100

  @util.canonicalize_domain
  def get(self):
    # only cache the first page
    return super(UsersHandler, self).get(cache=not self.request.params)

  def template_file(self):
    return 'users.html'

  def template_vars(self):
    start_name = self.request.get('start_name')
    queries = [cls.query(cls.name >= start_name).fetch_async(self.PAGE_SIZE)
               for cls in models.sources.values()]

    sources = sorted(itertools.chain(*[q.get_result() for q in queries]),
                     key=lambda s: (s.name.lower(), s.GR_CLASS.NAME))
    sources = [self.preprocess_source(s) for s in sources
               if s.name.lower() >= start_name.lower() and s.features
                  and s.status != 'disabled'
               ][:self.PAGE_SIZE]

    vars = super(UsersHandler, self).template_vars()
    vars.update({
        'sources': sources,
        'string': string,  # module, used in template
        'PAGE_SIZE': self.PAGE_SIZE,
        })
    return vars


class UserHandler(DashboardHandler):
  """Handler for a user page."""

  @util.canonicalize_domain
  def get(self, source_short_name, id):
    cls = models.sources[source_short_name]
    self.source = cls.lookup(id)

    if not self.source:
      id = id.decode('utf-8')
      key = cls.query(ndb.OR(*[ndb.GenericProperty(prop) == id for prop in
                               ('domains', 'inferred_username', 'name', 'username')])
                      ).get(keys_only=True)
      if key:
        return self.redirect(cls(key=key).bridgy_path(), permanent=True)

    if self.source and self.source.features:
      self.source.verify()
      self.source = self.preprocess_source(self.source)
    else:
      self.response.status_int = 404
    super(UserHandler, self).get()

  def template_file(self):
    return ('%s_user.html' % self.source.SHORT_NAME
            if self.source and self.source.features
            else 'user_not_found.html')

  def headers(self):
    """Override the default and omit Cache-Control."""
    return {'Access-Control-Allow-Origin': '*'}

  def template_vars(self):
    vars = super(UserHandler, self).template_vars()
    vars.update({
      'source': self.source,
      'sources': models.sources,
      'EPOCH': util.EPOCH,
      'REFETCH_HFEED_TRIGGER': models.REFETCH_HFEED_TRIGGER,
      'RECENT_PRIVATE_POSTS_THRESHOLD': RECENT_PRIVATE_POSTS_THRESHOLD,
    })
    if not self.source:
      return vars

    if isinstance(self.source, instagram.Instagram):
      auth = self.source.auth_entity
      vars['indieauth_me'] = (
        auth.key.id() if isinstance(auth, indieauth.IndieAuth)
        else self.source.domain_urls[0] if self.source.domain_urls
        else None)

    # Blog webmention promos
    if 'webmention' not in self.source.features:
      if self.source.SHORT_NAME in ('blogger', 'medium', 'tumblr', 'wordpress'):
        vars[self.source.SHORT_NAME + '_promo'] = True
      else:
        for domain in self.source.domains:
          if ('.blogspot.' in domain and  # Blogger uses country TLDs
              not Blogger.query(Blogger.domains == domain).get()):
            vars['blogger_promo'] = True
          elif (domain.endswith('tumblr.com') and
                not Tumblr.query(Tumblr.domains == domain).get()):
            vars['tumblr_promo'] = True
          elif (domain.endswith('wordpress.com') and
                not WordPress.query(WordPress.domains == domain).get()):
            vars['wordpress_promo'] = True

    # Responses
    if 'listen' in self.source.features or 'email' in self.source.features:
      vars['responses'] = []
      query = Response.query().filter(Response.source == self.source.key)

      # if there's a paging param (responses_before or responses_after), update
      # query with it
      def get_paging_param(param):
        val = self.request.get(param)
        try:
          return util.parse_iso8601(val) if val else None
        except:
          msg = "Couldn't parse %s %r as ISO8601" % (param, val)
          logging.warning(msg, exc_info=True)
          self.abort(400, msg)

      before = get_paging_param('responses_before')
      after = get_paging_param('responses_after')
      if before and after:
        self.abort(400, "can't handle both responses_before and responses_after")
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
            a['content'] = activity_content = a.get('object', {}).get('content') or ''

        response_content = r.response.get('content')
        if (not response_content or
            (r.type == 'repost' and activity_content.startswith(response_content))):
          phrases = {
            'like': 'liked this',
            'repost': 'reposted this',
            'rsvp-yes': 'is attending',
            'rsvp-no': 'is not attending',
            'rsvp-maybe': 'might attend',
            'rsvp-interested': 'is interested',
            'invite': 'is invited',
          }
          r.response['content'] = '%s %s.' % (
            r.actor.get('displayName') or '',
            phrases.get(r.type) or phrases.get(verb))

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
          p.key.parent().id().decode('utf-8'),
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


class DeleteStartHandler(util.Handler):
  def post(self):
    source = self.load_source(param='key')
    kind = source.key.kind()
    feature = util.get_required_param(self, 'feature')
    state = util.encode_oauth_state({
      'operation': 'delete',
      'feature': feature,
      'source': source.key.urlsafe(),
      'callback': self.request.get('callback'),
    })

    # Blogger don't support redirect_url() yet
    if kind == 'Blogger':
      return self.redirect('/blogger/delete/start?state=%s' % state)

    path = ('/instagram/callback' if kind == 'Instagram'
            else '/wordpress/add' if kind == 'WordPress'
            else '/%s/delete/finish' % source.SHORT_NAME)
    kwargs = {}
    if kind == 'Twitter':
      kwargs['access_type'] = 'read' if feature == 'listen' else 'write'

    start_handler = (indieauth.StartHandler if kind == 'Instagram'
                     else source.OAUTH_START_HANDLER)
    handler = start_handler.to(path, **kwargs)(self.request, self.response)
    try:
      self.redirect(handler.redirect_url(state=state))
    except Exception as e:
      code, body = util.interpret_http_exception(e)
      if not code and util.is_connection_failure(e):
        code = '-'
        body = str(e)
      if code:
        self.messages.add('%s API error %s: %s' % (source.GR_CLASS.NAME, code, body))
        self.redirect(source.bridgy_url(self))
      else:
        raise


class DeleteFinishHandler(util.Handler):
  def get(self):
    parts = util.decode_oauth_state(self.request.get('state') or '')
    callback = parts and parts.get('callback')

    if self.request.get('declined'):
      # disable declined means no change took place
      if callback:
        callback = util.add_query_params(callback, {'result': 'declined'})
        self.redirect(callback.encode('utf-8'))
      else:
        self.messages.add('If you want to disable, please approve the prompt.')
        self.redirect('/')
      return

    if (not parts or 'feature' not in parts or 'source' not in parts):
      self.abort(400, 'state query parameter must include "feature" and "source"')

    feature = parts['feature']
    if feature not in (Source.FEATURES):
      self.abort(400, 'cannot delete unknown feature %s' % feature)

    logged_in_as = ndb.Key(
      urlsafe=util.get_required_param(self, 'auth_entity')).get()
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
          'key': source.key.urlsafe(),
        })
      else:
        self.messages.add('Disabled %s for %s. Sorry to see you go!' %
                          (noun, source.label()))
      # util.email_me(subject='Deleted Bridgy %s user: %s %s' %
      #               (feature, source.label(), source.key.string_id()),
      #               body=source.bridgy_url(self))
    else:
      if callback:
        callback = util.add_query_params(callback, {'result': 'failure'})
      else:
        self.messages.add('Please log into %s as %s to disable it here.' %
                          (source.GR_CLASS.NAME, source.name))

    self.redirect(callback.encode('utf-8') if callback
                  else source.bridgy_url(self) if source.features
                  else '/')


class PollNowHandler(util.Handler):
  source = None

  def post(self):
    self.get_source()
    util.add_poll_task(self.source, now=True)
    self.messages.add("Polling now. Refresh in a minute to see what's new!")
    self.redirect(self.source.bridgy_url(self))

  def get_source(self):
    if not self.source:
      self.source = self.load_source(param='key')
    return self.source


class CrawlNowHandler(PollNowHandler):
  def post(self):
    self.setup_refetch_hfeed()
    util.add_poll_task(self.source, now=True)
    self.messages.add("Crawling now. Refresh in a minute to see what's new!")
    self.redirect(self.source.bridgy_url(self))

  @ndb.transactional
  def setup_refetch_hfeed(self):
    self.get_source()
    self.source.last_hfeed_refetch = models.REFETCH_HFEED_TRIGGER
    self.source.last_feed_syndication_url = None
    self.source.put()


class RetryHandler(util.Handler):
  def post(self):
    entity = self.load_source(param='key')
    if not isinstance(entity, Webmentions):
      self.abort(400, 'Unexpected key kind %s', entity.key.kind())

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
    self.redirect(self.request.get('redirect_to').encode('utf-8') or
                  entity.source.get().bridgy_url(self))


class DiscoverHandler(util.Handler):
  def post(self):
    source = self.load_source()

    # validate URL, find silo post
    url = util.get_required_param(self, 'url')
    domain = util.domain_from_link(url)
    path = urllib.parse.urlparse(url).path
    msg = 'Discovering now. Refresh in a minute to see the results!'

    if domain == source.GR_CLASS.DOMAIN:
      post_id = source.GR_CLASS.post_id(url)
      if post_id:
        type = 'event' if path.startswith('/events/') else None
        util.add_discover_task(source, post_id, type=type)
      else:
        msg = "Sorry, that doesn't look like a %s post URL." % source.GR_CLASS.NAME

    elif util.domain_or_parent_in(domain, source.domains):
      synd_links = original_post_discovery.process_entry(source, url, {}, False, [])
      if synd_links:
        for link in synd_links:
          util.add_discover_task(source, source.GR_CLASS.post_id(link))
        source.updates = {'last_syndication_url': util.now_fn()}
        models.Source.put_updates(source)
      else:
        msg = 'Failed to fetch %s or find a %s syndication link.' % (
          util.pretty_link(url), source.GR_CLASS.NAME)

    else:
      msg = 'Please enter a URL on either your web site or %s.' % source.GR_CLASS.NAME

    self.messages.add(msg)
    self.redirect(source.bridgy_url(self))


class EditWebsites(webutil_handlers.TemplateHandler, util.Handler):

  def template_file(self):
    return 'edit_websites.html'

  def content_type(self):
    return 'text/html; charset=utf-8'

  def post(self):
    source = self.load_source()
    redirect_url = '%s?%s' % (self.request.path, urllib.parse.urlencode({
      'source_key': source.key.urlsafe(),
    }))

    add = self.request.get('add')
    delete = self.request.get('delete')
    if (add and delete) or (not add and not delete):
      self.abort(400, 'Either add or delete param (but not both) required')

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
        self.abort(400, "%s not found in %s's current web sites" % (
                          delete, source.label()))
      domain = util.domain_from_link(delete)
      if domain not in set(util.domain_from_link(url) for url in source.domain_urls):
        source.domains.remove(domain)
      source.put()
      self.messages.add('Removed %s.' % link)

    self.redirect(redirect_url)

  def template_vars(self):
    return {
      'source': self.preprocess_source(self.load_source()),
      'util': util,
    }


class RedirectToFrontPageHandler(util.Handler):
  @util.canonicalize_domain
  def get(self, feature):
    """Redirect to the front page."""
    self.redirect(util.add_query_params('/', self.request.params.items()),
                  permanent=True)

  head = get


class LogoutHandler(util.Handler):
  @util.canonicalize_domain
  def get(self):
    """Redirect to the front page."""
    self.set_logins([])
    self.messages.add('Logged out.')
    self.redirect('/')


class WarmupHandler(util.Handler):
  """Warmup requests. Noop.

  https://developers.google.com/appengine/docs/python/config/appconfig#Python_app_yaml_Warmup_requests
  """
  def get(self):
    pass


class CspReportHandler(util.Handler):
  """Log Content-Security-Policy reports. https://content-security-policy.com/"""
  def post(self):
    logging.info(self.request.body)


class FacebookIsDeadHandler(util.Handler):
  def get(self):
    self.redirect('/about#rip-facebook')


class GooglePlusIsDeadHandler(util.Handler):
  def get(self):
    self.redirect('/about#rip-google+')



application = webapp2.WSGIApplication(
  [('/?', FrontPageHandler),
   ('/users/?', UsersHandler),
   ('/(blogger|fake|fake_blog|flickr|github|instagram|mastodon|medium|tumblr|twitter|wordpress)/([^/]+)/?',
    UserHandler),
   ('/facebook/.*', FacebookIsDeadHandler),
   ('/googleplus/.*', GooglePlusIsDeadHandler),
   ('/about/?', AboutHandler),
   ('/delete/start', DeleteStartHandler),
   ('/delete/finish', DeleteFinishHandler),
   ('/discover', DiscoverHandler),
   ('/poll-now', PollNowHandler),
   ('/crawl-now', CrawlNowHandler),
   ('/retry', RetryHandler),
   ('/(listen|publish)/?', RedirectToFrontPageHandler),
   ('/edit-websites', EditWebsites),
   ('/logout', LogoutHandler),
   ('/csp-report', CspReportHandler),
   ('/_ah/warmup', WarmupHandler),
   ], debug=appengine_config.DEBUG)
