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

# need to import modules with model class definitions, e.g. facebook, for
# template rendering.
import appengine_config
from activitystreams.oauth_dropins import facebook as oauth_facebook
from activitystreams.oauth_dropins import googleplus as oauth_googleplus
from activitystreams.oauth_dropins import instagram as oauth_instagram
from activitystreams.oauth_dropins import twitter as oauth_twitter
from facebook import FacebookPage
from googleplus import GooglePlusPage
from instagram import Instagram
from twitter import Twitter
from twitter_search import TwitterSearch
import models
import util
from webutil import handlers

from google.appengine.api import mail
from google.appengine.api import users
from google.appengine.ext import db
from google.appengine.ext.webapp import template
import webapp2


def update_scheme(url):
  # Instagram doesn't serve images over SSL, so switch to their S3 URL. (The
  # http is converted to https by update_scheme()).
  # https://groups.google.com/d/msg/instagram-api-developers/fB4mwYXZF1c/q9n9gPO11JQJ
  url = re.sub('^http://images\.(ak\.)instagram\.com',
               'http://distillery.s3.amazonaws.com', url)
  return util.update_scheme(url)


class DashboardHandler(util.Handler):
  def get(self):
    """Renders the dashboard.

    Args:
      msg: string, message to be displayed
    """
    sources = {str(source.key()): source for source in
               itertools.chain(FacebookPage.all().run(), Twitter.all().run(),
                               GooglePlusPage.all().run(), Instagram.all().run())}

    # manually update the source we just added or deleted to workaround
    # inconsistent global queries.
    added = self.request.get('added')
    if added and added not in sources:
      sources[added] = db.get(added)
    deleted = self.request.get('deleted')
    if deleted in sources:
      del sources[deleted]

    # kick off queries for recent responses for each source. all queries run
    # async, in parallel.
    for source in sources.values():
      source.recent_responses = source.response_set.order('-updated').run(limit=5)

    # now wait on query results
    for source in sources.values():
      # convert image URL to https if we're serving over SSL
      source.picture = update_scheme(source.picture)
      source.recent_responses = list(source.recent_responses)
      source.recent_response_status = None
      for c in source.recent_responses:
        c.response = json.loads(c.response_json)
        c.activity = json.loads(c.activity_json)
        c.response['published'] = util.parse_iso8601(c.response['published'])

        # convert image URL to https if we're serving over SSL
        image_url = c.response['author'].setdefault('image', {}).get('url')
        if image_url:
          c.response['author']['image']['url'] = update_scheme(image_url)

        # generate original post links
        def link(url, glyphicon=''):
          parsed = urlparse.urlparse(url)
          snippet = url[len(parsed.scheme) + 3:]  # strip scheme
          max_len = max(20, len(parsed.netloc) + 1)
          if len(snippet) > max_len + 3:
            snippet = snippet[:max_len] + '...'
          if glyphicon:
            glyphicon = '<span class="glyphicon glyphicon-%s"></span>' % glyphicon
          return ('<a target="_blank" class="original-post" href="%s">%s %s</a>'
                  % (url, snippet, glyphicon))

        c.links = (
          set(link(url, 'exclamation-sign') for url in c.error) |
          set(link(url, 'transfer') for url in c.unsent if url not in c.error) |
          set(link(url) for url in c.sent if url not in (c.error + c.unsent)))

        if c.error:
          source.recent_response_status = 'error'
        elif c.unsent and not source.recent_response_status:
          source.recent_response_status = 'processing'

    # sort sources by name
    sources = sorted(sources.values(), key=lambda s: (s.name.lower(), s.DISPLAY_NAME))

    msgs = [urllib.unquote_plus(m) for m in set(self.request.params.getall('msg'))]
    path = os.path.join(os.path.dirname(__file__), 'templates', 'dashboard.html')

    self.response.headers['Link'] = ('<%s/webmention>; rel="webmention"' %
                                     self.request.host_url)
    self.response.out.write(template.render(path, {
          'sources': sources, 'msgs': msgs, 'epoch': util.EPOCH}))


class AboutHandler(handlers.TemplateHandler):
  def template_file(self):
    return os.path.join(os.path.dirname(__file__), 'templates', 'about.html')


class DeleteStartHandler(util.Handler):
  OAUTH_MODULES = {
    'FacebookPage': oauth_facebook,
    'GooglePlusPage': oauth_googleplus,
    'Instagram': oauth_instagram,
    'Twitter': oauth_twitter,
    }

  def post(self):
    key = util.get_required_param(self, 'key')
    source = db.get(key)
    module = self.OAUTH_MODULES[source.kind()]

    if module is oauth_googleplus:
      # Google+ doesn't support redirect_url() yet
      self.redirect('/googleplus/delete/start?state=%s' % key)
    else:
      if module is oauth_instagram:
        path = '/instagram/oauth_callback'
      else:
        path = '/%s/delete/finish' % source.SHORT_NAME
      handler = module.StartHandler.to(path)(self.request, self.response)
      self.redirect(handler.redirect_url(state=key))


class DeleteFinishHandler(util.Handler):
  def get(self):
    logged_in_as = util.get_required_param(self, 'auth_entity')
    source = db.get(util.get_required_param(self, 'state'))
    source_auth_entity = models.Source.auth_entity.get_value_for_datastore(source)
    if logged_in_as == str(source_auth_entity):
      # TODO: remove credentials, tasks, etc.
      source.delete()
      self.messages.add('Deleted %s.' % source.label())
      mail.send_mail(sender='delete@brid-gy.appspotmail.com',
                     to='webmaster@brid.gy',
                     subject='Deleted Brid.gy user: %s %s' %
                     (source.label(), source.key().name()),
                     body='%s/#%s' % (self.request.host_url, source.dom_id()))
      self.redirect('/?deleted=%s' % source.key())
    else:
      self.messages.add('Please log into %s as %s to delete it here.' %
                        (source.DISPLAY_NAME, source.name))
      self.redirect('/#%s' % source.dom_id())


application = webapp2.WSGIApplication(
  [('/', DashboardHandler),
   ('/about', AboutHandler),
   ('/delete/start', DeleteStartHandler),
   ('/delete/finish', DeleteFinishHandler),
   ], debug=appengine_config.DEBUG)
