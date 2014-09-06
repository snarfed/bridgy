"""Tumblr + Disqus blog webmention implementation.

To use, go to your Tumblr dashboard, click Customize, Edit HTML, then put this
in the head section:

<link rel="webmention" href="https://www.brid.gy/webmention/tumblr">

http://disqus.com/api/docs/
http://disqus.com/api/docs/posts/create/
https://github.com/disqus/DISQUS-API-Recipes/blob/master/snippets/php/create-guest-comment.php
http://help.disqus.com/customer/portal/articles/466253-what-html-tags-are-allowed-within-comments-
create returns id, can lookup by id w/getContext?

guest post (w/arbitrary author, url):
http://spirytoos.blogspot.com/2013/12/not-so-easy-posting-as-guest-via-disqus.html
http://stackoverflow.com/questions/15416688/disqus-api-create-comment-as-guest
http://jonathonhill.net/2013-07-11/disqus-guest-posting-via-api/

can send url and not look up disqus thread id!
http://stackoverflow.com/questions/4549282/disqus-api-adding-comment
https://disqus.com/api/docs/forums/listThreads/

test command line:
curl localhost:8080/webmention/tumblr \
  -d 'source=http://localhost/response.html&target=http://snarfed.tumblr.com/post/60428995188/glen-canyon-http-t-co-fzc4ehiydp?foo=bar#baz'
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import collections
import datetime
import json
import logging
import os
import re
import requests
import urllib
import urlparse
from webob import exc

import appengine_config
from appengine_config import HTTP_TIMEOUT

from activitystreams.oauth_dropins import tumblr as oauth_tumblr
import models
import requests
import superfeedr
import util

from google.appengine.ext import ndb
from google.appengine.ext.webapp import template
import webapp2

TUMBLR_AVATAR_URL = 'http://api.tumblr.com/v2/blog/%s/avatar/512'
DISQUS_API_CREATE_POST_URL = 'https://disqus.com/api/3.0/posts/create.json'
DISQUS_API_THREAD_DETAILS_URL = 'http://disqus.com/api/3.0/threads/details.json'

# Tumblr has no single standard markup or JS for integrating Disqus. It does
# have a default way, but themes often do it themselves, differently. Sigh.
# Details in https://github.com/snarfed/bridgy/issues/278
DISQUS_SHORTNAME_RE = re.compile("""
    (?:http://disqus.com/forums|disqus[ -_]?(?:user|short)?name)
    \ *[=:/]\ *['"]?
    ([^/"\' ]+)     # the actual shortname
    """,
  re.IGNORECASE | re.VERBOSE)

class Tumblr(models.Source):
  """A Tumblr blog.

  The key name is the blog domain.
  """
  AS_CLASS = collections.namedtuple('FakeAsClass', ('NAME',))(NAME='Tumblr')
  SHORT_NAME = 'tumblr'

  disqus_shortname = ndb.StringProperty()

  def feed_url(self):
    # http://www.tumblr.com/help  (search for feed)
    return urlparse.urljoin(self.domain_urls[0], '/rss')

  def silo_url(self):
    return self.domain_urls[0]

  def edit_template_url(self):
    return 'http://www.tumblr.com/customize/%s' % self.auth_entity.id()

  @staticmethod
  def new(handler, auth_entity=None, blog_name=None, **kwargs):
    """Creates and returns a Tumblr for the logged in user.

    Args:
      handler: the current RequestHandler
      auth_entity: oauth_dropins.tumblr.TumblrAuth
      blog_name: which blog. optional. passed to _url_and_domain.
    """
    url, domain, ok = Tumblr._url_and_domain(auth_entity, blog_name=blog_name)
    if not ok:
      handler.messages = {'Tumblr blog not found. Please create one first!'}
      return None

    return Tumblr(id=domain,
                  auth_entity=auth_entity.key,
                  domains=[domain],
                  domain_urls=[url],
                  name=auth_entity.user_display_name(),
                  picture=TUMBLR_AVATAR_URL % domain,
                  superfeedr_secret=util.generate_secret(),
                  **kwargs)

  @staticmethod
  def _url_and_domain(auth_entity, blog_name=None):
    """Returns the blog URL and domain.

    Args:
      auth_entity: oauth_dropins.tumblr.TumblrAuth
      blog_name: which blog. optional. matches the 'name' field for one of the
        blogs in auth_entity.user_json['user']['blogs'].

    Returns: (string url, string domain, boolean ok)
    """
    for blog in json.loads(auth_entity.user_json).get('user', {}).get('blogs', []):
      if ((blog_name and blog_name == blog.get('name')) or
          (not blog_name and blog.get('primary'))):
        return blog['url'], util.domain_from_link(blog['url']), True

    return None, None, False

  def verified(self):
    """Returns True if we've found the webmention endpoint and Disqus."""
    return self.webmention_endpoint and self.disqus_shortname

  def verify(self):
    """Checks that Disqus is installed as well as the webmention endpoint.

    Stores the result in webmention_endpoint. Expects that Source.verify
    sets the self._fetched_html attr.
    """
    if self.verified():
      return

    super(Tumblr, self).verify(force=True)

    if not self.disqus_shortname and self._fetched_html:
      # scrape the disqus shortname out of the page
      logging.info("Looking for Disqus shortname in fetched HTML")
      match = DISQUS_SHORTNAME_RE.search(self._fetched_html)
      if match:
        self.disqus_shortname = match.group(1)
        logging.info("Found Disqus shortname %s", self.disqus_shortname)
        self.put()

  def create_comment(self, post_url, author_name, author_url, content):
    """Creates a new comment in the source silo.

    Must be implemented by subclasses.

    Args:
      post_url: string
      author_name: string
      author_url: string
      content: string

    Returns: JSON response dict with 'id' and other fields
    """
    if not self.disqus_shortname:
      raise exc.HTTPBadRequest("Your Bridgy account isn't fully set up yet: "
                               "we haven't found your Disqus account.")

    # strip slug, query and fragment from post url
    parsed = urlparse.urlparse(post_url)
    path = parsed.path.split('/')
    try:
      tumblr_post_id = int(path[-1])
    except ValueError:
      path.pop(-1)
    post_url = urlparse.urlunparse(parsed[:2] + ('/'.join(path), '', '', ''))

    # get the disqus thread id. details on thread queries:
    # http://stackoverflow.com/questions/4549282/disqus-api-adding-comment
    # https://disqus.com/api/docs/threads/details/
    resp = self.disqus_call(requests.get, DISQUS_API_THREAD_DETAILS_URL,
                            {'forum': self.disqus_shortname,
                             # ident:[tumblr_post_id] should work, but doesn't :/
                             'thread': 'link:%s' % post_url,
                             })
    thread_id = resp['id']

    # create the comment
    message = u'<a href="%s">%s</a>: %s' % (author_url, author_name, content)
    resp = self.disqus_call(requests.post, DISQUS_API_CREATE_POST_URL,
                            {'thread': thread_id,
                             'message': message.encode('utf-8'),
                             # only allowed when authed as moderator/owner
                             # 'state': 'approved',
                             })
    return resp

  @staticmethod
  def disqus_call(method, url, params, **kwargs):
    """Makes a Disqus API call.

    Args:
      method: requests function to use, e.g. requests.get
      url: string
      params: query parameters
      kwargs: passed through to method

    Returns: dict, JSON response
    """
    logging.info('Calling Disqus %s with %s', url.split('/')[-2:], params)
    params.update({
        'api_key': appengine_config.DISQUS_API_KEY,
        'api_secret': appengine_config.DISQUS_API_SECRET,
        'access_token': appengine_config.DISQUS_ACCESS_TOKEN,
        })
    resp = method(url, timeout=HTTP_TIMEOUT, params=params, **kwargs)
    resp.raise_for_status()
    resp = resp.json().get('response', {})
    logging.info('Response: %s', resp)
    return resp


class ChooseBlog(oauth_tumblr.CallbackHandler, util.Handler):
  def finish(self, auth_entity, state=None):
    if not auth_entity:
      self.maybe_add_or_delete_source(Tumblr, auth_entity, state)
      return

    vars = {
      'action': '/tumblr/add',
      'state': state,
      'auth_entity_key': auth_entity.key.urlsafe(),
      'blogs': [{'id': b['name'],
                 'title': b.get('title', ''),
                 'domain': util.domain_from_link(b['url'])}
                # user_json is the user/info response:
                # http://www.tumblr.com/docs/en/api/v2#user-methods
                for b in json.loads(auth_entity.user_json)['user']['blogs']
                if b.get('name') and b.get('url')],
      }
    logging.info('Rendering choose_blog.html with %s', vars)

    self.response.headers['Content-Type'] = 'text/html'
    self.response.out.write(template.render('templates/choose_blog.html', vars))


class AddTumblr(util.Handler):
  def post(self):
    auth_entity_key = util.get_required_param(self, 'auth_entity_key')
    self.maybe_add_or_delete_source(
      Tumblr,
      ndb.Key(urlsafe=auth_entity_key).get(),
      util.get_required_param(self, 'state'),
      blog_name=util.get_required_param(self, 'blog'),
      )


class SuperfeedrNotifyHandler(superfeedr.NotifyHandler):
  SOURCE_CLS = Tumblr


application = webapp2.WSGIApplication([
    ('/tumblr/start', oauth_tumblr.StartHandler.to('/tumblr/choose_blog')),
    ('/tumblr/choose_blog', ChooseBlog),
    ('/tumblr/add', AddTumblr),
    ('/tumblr/delete/finish', oauth_tumblr.CallbackHandler.to('/delete/finish')),
    ('/tumblr/notify/(.+)', SuperfeedrNotifyHandler),
    ], debug=appengine_config.DEBUG)
