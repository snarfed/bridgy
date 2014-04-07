"""Bridgy user-facing handlers: front page, user pages, and delete POSTs.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import itertools
import json
import logging
import os
import re
import urllib
import urlparse

import appengine_config

# need to import modules with model class definitions, e.g. facebook, for
# template rendering.
from activitystreams.oauth_dropins import facebook as oauth_facebook
from activitystreams.oauth_dropins import googleplus as oauth_googleplus
from activitystreams.oauth_dropins import instagram as oauth_instagram
from activitystreams.oauth_dropins import twitter as oauth_twitter
from facebook import FacebookPage
from googleplus import GooglePlusPage
from instagram import Instagram
from twitter import Twitter
import handlers
from models import Publish, Response, Source
import util
from activitystreams.oauth_dropins.webutil.handlers import TemplateHandler

from google.appengine.api import memcache
from google.appengine.api import users
from google.appengine.ext import ndb
from google.appengine.ext.webapp import template
import webapp2


class DashboardHandler(TemplateHandler, util.Handler):
  """Base handler for both the front page and user pages."""

  def head(self, *args, **kwargs):
    """Return an empty 200 with no caching directives."""

  def post(self, *args, **kwargs):
    """Facebook uses a POST instead of a GET when it renders us in Canvas.

    http://stackoverflow.com/a/5353413/186123
    """
    return self.get(*args, **kwargs)

  def content_type(self):
    return 'text/html; charset=utf-8'


class FrontPageHandler(DashboardHandler):
  """Handler for the front page."""

  def template_file(self):
    return 'templates/index.html'

  def get(self):
    if appengine_config.DEBUG:
      # don't cache front page when running in in dev_appserver
      super(DashboardHandler, self).get()

    self.response.headers['Content-Type'] = self.content_type()
    cached = util.CachedFrontPage.load()
    if cached:
      self.response.write(cached.html)
    else:
      super(DashboardHandler, self).get()
      util.CachedFrontPage.store(self.response.body)

  def template_vars(self):
    queries = [cls.query() for cls in handlers.SOURCES.values()]
    sources = {source.key.urlsafe(): source for source in itertools.chain(*queries)}

    # preprocess sources, sort by name
    sources = sorted([self.preprocess_source(s) for s in sources.values()],
                     key=lambda s: (s.name.lower(), s.AS_CLASS.NAME))
    vars = super(FrontPageHandler, self).template_vars()
    vars['sources'] = sources
    return vars


class UserHandler(DashboardHandler):
  """Handler for a user page."""

  def get(self, source_short_name, id):
    self.source = ndb.Key(handlers.SOURCES[source_short_name], id).get()
    if self.source:
      self.source = self.preprocess_source(self.source)
    else:
      self.response.status_int = 404
    super(UserHandler, self).get()

  def template_file(self):
    return 'templates/user.html' if self.source else 'templates/user_not_found.html'

  def template_vars(self):
    if not self.source:
      return {}

    # Responses
    responses = Response.query().filter(Response.source == self.source.key)\
                                .order(-Response.created)\
                                .fetch(10)
    for r in responses:
      r.response = json.loads(r.response_json)
      r.activity = json.loads(r.activity_json)
      r.actor = r.response.get('author') or r.response.get('actor', {})
      if not r.response.get('content'):
        if r.type == 'like':
          r.response['content'] = '%s liked' % r.actor.get('displayName', '-');
        elif r.type == 'repost':
          r.response['content'] = '%s reposted' % r.actor.get('displayName', '-');

      # convert image URL to https if we're serving over SSL
      image_url = r.actor.setdefault('image', {}).get('url')
      if image_url:
        r.actor['image']['url'] = util.update_scheme(image_url, self)

      # generate original post links
      link = lambda url, g: util.pretty_link(
        url, glyphicon=g, a_class='original-post', new_tab=True)
      r.links = util.trim_nulls({
        'Failed': set(link(url, 'exclamation-sign') for url in r.error + r.failed),
        'Sending': set(link(url, 'transfer') for url in r.unsent
                       if url not in r.error),
        'Sent': set(link(url, None) for url in r.sent
                    if url not in (r.error + r.unsent)),
        'No webmention support': set(link(url, None) for url in r.skipped),
        })

      # XXX horrible hack: use created time as updated time if updated time is
      # before 2014-03-28 evening...
      #
      # ...to band-aid the fact that a data migration accidentally set all
      # responses' updated time to then. :/ details in
      # https://github.com/snarfed/bridgy/issues/68
      if r.updated <= datetime.datetime(2014, 3, 29):
        r.updated = r.created

    responses.sort(key=lambda r: r.updated, reverse=True)

    # Publishes
    publishes = Publish.query().filter(Publish.source == self.source.key)\
                               .order(-Publish.updated)\
                               .fetch(10)
    for p in publishes:
      p.pretty_page = util.pretty_link(
        p.key.parent().id(), a_class='original-post', new_tab=True)

    vars = super(UserHandler, self).template_vars()
    vars.update({'source': self.source,
                 'responses': responses,
                 'publishes': publishes,
                 'epoch': util.EPOCH,
                 })
    return vars


class AboutHandler(TemplateHandler):
  def head(self):
    """Return an empty 200 with no caching directives."""

  def template_file(self):
    return 'templates/about.html'


class DeleteStartHandler(util.Handler):
  OAUTH_MODULES = {
    'FacebookPage': oauth_facebook,
    'GooglePlusPage': oauth_googleplus,
    'Instagram': oauth_instagram,
    'Twitter': oauth_twitter,
    }

  def post(self):
    key = ndb.Key(urlsafe=util.get_required_param(self, 'key'))
    module = self.OAUTH_MODULES[key.kind()]
    state = '%s-%s' % (util.get_required_param(self, 'feature'), key.urlsafe())

    if module is oauth_googleplus:
      # Google+ doesn't support redirect_url() yet
      self.redirect('/googleplus/delete/start?state=%s' % state)
    else:
      if module is oauth_instagram:
        path = '/instagram/oauth_callback'
      else:
        path = '/%s/delete/finish' % key.get().SHORT_NAME
      handler = module.StartHandler.to(path)(self.request, self.response)
      self.redirect(handler.redirect_url(state=state))


class DeleteFinishHandler(util.Handler):
  def get(self):
    parts = util.get_required_param(self, 'state').split('-', 1)
    feature = parts[0]
    if len(parts) != 2 or feature not in ('listen', 'publish'):
      self.error(400, 'state query parameter must be [FEATURE]-[SOURCE KEY]')

    if self.request.get('declined'):
      self.messages.add("OK, you're still signed up.")
      self.redirect('/')
      return

    logged_in_as = util.get_required_param(self, 'auth_entity')
    source = ndb.Key(urlsafe=parts[1]).get()
    if logged_in_as == source.auth_entity.urlsafe():
      # TODO: remove credentials
      if feature in source.features:
        source.features.remove(feature)
        source.put()
      self.messages.add('Disabled %sing for %s. Sorry to see you go!' %
                        (feature, source.label()))
      util.email_me(subject='Deleted Bridgy %s user: %s %s' %
                    (feature, source.label(), source.key.string_id()),
                    body=source.bridgy_url(self))
    else:
      self.messages.add('Please log into %s as %s to delete it here.' %
                        (source.AS_CLASS.NAME, source.name))

    self.redirect(source.bridgy_url(self))


class PollNowHandler(util.Handler):
  def post(self):
    source = ndb.Key(urlsafe=util.get_required_param(self, 'key')).get()
    if not source:
      self.abort(400, 'source not found')

    util.add_poll_task(source)
    self.messages.add("Polling now. Refresh in a minute to see what's new!")
    self.redirect(source.bridgy_url(self))


class RetryResponseHandler(util.Handler):
  def post(self):
    response = ndb.Key(urlsafe=util.get_required_param(self, 'key')).get()
    if not response:
      self.abort(400, 'response not found')

    util.add_propagate_task(response)
    self.messages.add('Retrying. Refresh in a minute to see the results!')
    self.redirect(response.source.get().bridgy_url(self))


class RedirectToFrontPageHandler(util.Handler):
  def get(self, feature):
    """Redirect to the front page."""
    self.redirect(util.add_query_params('/', self.request.params.items()),
                  permanent=True)

  head = get


application = webapp2.WSGIApplication(
  [('/?', FrontPageHandler),
   ('/(facebook|googleplus|instagram|twitter)/(.+)/?', UserHandler),
   ('/about/?', AboutHandler),
   ('/delete/start', DeleteStartHandler),
   ('/delete/finish', DeleteFinishHandler),
   ('/poll-now', PollNowHandler),
   ('/retry-response', RetryResponseHandler),
   ('/(listen|publish)/?', RedirectToFrontPageHandler),
   ], debug=appengine_config.DEBUG)
