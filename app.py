"""Bridgy front page/dashboard.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

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
from models import Response, Source
import util
from activitystreams.oauth_dropins.webutil.handlers import TemplateHandler

from google.appengine.api import mail
from google.appengine.api import users
from google.appengine.ext import ndb
from google.appengine.ext.webapp import template
import webapp2


def source_dom_id_to_key(id):
  """Parses a string returned by Source.dom_id() and returns its ndb.Key."""
  short_name, string_id = id.split('-', 1)
  return ndb.Key(handlers.SOURCES.get(short_name), string_id)


class DashboardHandler(TemplateHandler):
  def head(self):
    """Return an empty 200 with no caching directives."""

  def post(self):
    """Facebook uses a POST instead of a GET when it renders us in Canvas.

    http://stackoverflow.com/a/5353413/186123
    """
    return self.get()

  def content_type(self):
    return 'text/html; charset=utf-8'

  def template_vars(self):
    sources = {source.key.urlsafe(): source for source in
               itertools.chain(FacebookPage.query().iter(), Twitter.query().iter(),
                               GooglePlusPage.query().iter(), Instagram.query().iter())}

    # manually update the source we just added or deleted to workaround
    # inconsistent global queries.
    added = self.request.get('added')
    if added and added not in sources:
      sources[added] = ndb.Key(urlsafe=added).get()
    deleted = self.request.get('deleted')
    if deleted in sources:
      del sources[deleted]

    # tweak some fields, including converting image URLs to https if we're
    # serving over SSL
    for source in sources.values():
      if not source.name:
        source.name = source.key.string_id()
      source.picture = util.update_scheme(source.picture, self)

    # sort by name
    sources = sorted(sources.values(),
                     key=lambda s: (s.name.lower(), s.AS_CLASS.NAME))

    # force UTF-8 since the msg parameters were encoded as UTF-8 by
    # util.add_query_params().
    self.request.charset = 'utf-8'
    msgs = [m for m in set(self.request.params.getall('msg'))]

    # self.response.headers['Link'] = ('<%s/webmention>; rel="webmention"' %
    #                                  self.request.host_url)
    return {'sources': sources, 'msgs': msgs, 'epoch': util.EPOCH}


class ListenHandler(DashboardHandler):
  def template_file(self):
    return 'templates/listen.html'


class PublishHandler(DashboardHandler):
  def template_file(self):
    return 'templates/publish.html'


class ResponsesHandler(TemplateHandler):
  NO_RESULTS_HTTP_STATUS = 204

  def template_file(self):
    return 'templates/responses.html'

  def template_vars(self):
    key = source_dom_id_to_key(util.get_required_param(self, 'source'))
    responses = Response.query().filter(Response.source == key)\
                                .order(-Response.updated)\
                                .fetch(10)
    if not responses:
      self.error(self.NO_RESULTS_HTTP_STATUS)
      return {}

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
      def link(url, glyphicon=''):
        parsed = urlparse.urlparse(url)
        snippet = url[len(parsed.scheme) + 3:]  # strip scheme and leading www
        if snippet.startswith('www.'):
          snippet = snippet[4:]
        max_len = max(20, len(parsed.netloc) + 1)
        if len(snippet) > max_len + 3:
          snippet = snippet[:max_len] + '...'
        if glyphicon:
          glyphicon = '<span class="glyphicon glyphicon-%s"></span>' % glyphicon
        return ('<a target="_blank" class="original-post" href="%s">%s %s</a>'
                % (url, snippet, glyphicon))

      r.links = util.trim_nulls({
        'Failed': set(link(url, 'exclamation-sign') for url in r.error + r.failed),
        'Sending': set(link(url, 'transfer') for url in r.unsent
                       if url not in r.error),
        'Sent': set(link(url) for url in r.sent
                    if url not in (r.error + r.unsent)),
        'No webmention support': set(link(url) for url in r.skipped),
        })

      # ...left over from when responses were rendered in DashboardHandler.
      # consider reviving it someday.
      # if r.error:
      #   source.recent_response_status = 'error'
      # elif r.unsent and not source.recent_response_status:
      #   source.recent_response_status = 'processing'

    self.request.charset = 'utf-8'
    return {'responses': responses}


class AboutHandler(TemplateHandler):
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

    if module is oauth_googleplus:
      # Google+ doesn't support redirect_url() yet
      self.redirect('/googleplus/delete/start?state=%s' % key.urlsafe())
    else:
      if module is oauth_instagram:
        path = '/instagram/oauth_callback'
      else:
        path = '/%s/delete/finish' % key.get().SHORT_NAME
      handler = module.StartHandler.to(path)(self.request, self.response)
      self.redirect(handler.redirect_url(state=key.urlsafe()))


class DeleteFinishHandler(util.Handler):
  def get(self):
    if self.request.get('declined'):
      self.messages.add("OK, you're still signed up.")
      self.redirect('/')
      return

    logged_in_as = util.get_required_param(self, 'auth_entity')
    source = ndb.Key(urlsafe=util.get_required_param(self, 'state')).get()
    if logged_in_as == source.auth_entity.urlsafe():
      # TODO: remove credentials, tasks, etc.
      source.key.delete()
      self.messages.add('Deleted %s. Sorry to see you go!' % source.label())
      mail.send_mail(sender='delete@brid-gy.appspotmail.com',
                     to='webmaster@brid.gy',
                     subject='Deleted Bridgy user: %s %s' %
                     (source.label(), source.key.string_id()),
                     body='%s/#%s' % (self.request.host_url, source.dom_id()))
      self.redirect('/?deleted=%s' % source.key.urlsafe())
    else:
      self.messages.add('Please log into %s as %s to delete it here.' %
                        (source.AS_CLASS.NAME, source.name))
      self.redirect('/#%s' % source.dom_id())


application = webapp2.WSGIApplication(
  [('/', ListenHandler),
   ('/listen', ListenHandler),
   ('/publish', PublishHandler),
   ('/responses', ResponsesHandler),
   ('/about', AboutHandler),
   ('/delete/start', DeleteStartHandler),
   ('/delete/finish', DeleteFinishHandler),
   ], debug=appengine_config.DEBUG)
