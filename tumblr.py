"""Tumblr + Disqus blog webmention implementation.

To use, go to your Tumblr dashboard, click Customize, Edit HTML, then put this
in the head section:

<link rel="webmention" href="https://brid.gy/webmention/tumblr">

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
from __future__ import unicode_literals

from future import standard_library
standard_library.install_aliases()
import collections
import logging
import re
import urllib.parse
from webob import exc

import appengine_config

from google.cloud import ndb
from oauth_dropins import tumblr as oauth_tumblr
from oauth_dropins.webutil.handlers import JINJA_ENV
from oauth_dropins.webutil.util import json_dumps, json_loads
import webapp2

import models
import superfeedr
import util


TUMBLR_AVATAR_URL = 'http://api.tumblr.com/v2/blog/%s/avatar/512'
DISQUS_API_CREATE_POST_URL = 'https://disqus.com/api/3.0/posts/create.json'
DISQUS_API_THREAD_DETAILS_URL = 'http://disqus.com/api/3.0/threads/details.json'

# Tumblr has no single standard markup or JS for integrating Disqus. It does
# have a default way, but themes often do it themselves, differently. Sigh.
# Details in https://github.com/snarfed/bridgy/issues/278
DISQUS_SHORTNAME_RES = (
  re.compile("""
    (?:https?://disqus\.com/forums|disqus[ -_]?(?:user|short)?name)
    \ *[=:/]\ *['"]?
    ([^/"\' ]+)     # the actual shortname
    """, re.IGNORECASE | re.VERBOSE),
  re.compile('https?://([^./"\' ]+)\.disqus\.com/embed\.js'),
  )

class Tumblr(models.Source):
  """A Tumblr blog.

  The key name is the blog domain.
  """
  GR_CLASS = collections.namedtuple('FakeGrClass', ('NAME',))(NAME='Tumblr')
  OAUTH_START_HANDLER = oauth_tumblr.StartHandler
  SHORT_NAME = 'tumblr'

  disqus_shortname = ndb.StringProperty()

  def feed_url(self):
    # http://www.tumblr.com/help  (search for feed)
    return urllib.parse.urljoin(self.silo_url(), '/rss')

  def silo_url(self):
    return self.domain_urls[0]

  def edit_template_url(self):
    return 'http://www.tumblr.com/customize/%s' % self.auth_entity.id()

  @staticmethod
  def new(handler, auth_entity=None, blog_name=None, **kwargs):
    """Creates and returns a :class:`Tumblr` for the logged in user.

    Args:
      handler: the current :class:`webapp2.RequestHandler`
      auth_entity: :class:`oauth_dropins.tumblr.TumblrAuth`
      blog_name: which blog. optional. passed to _urls_and_domains.
    """
    urls, domains = Tumblr._urls_and_domains(auth_entity, blog_name=blog_name)
    if not urls or not domains:
      handler.messages = {'Tumblr blog not found. Please create one first!'}
      return None

    id = domains[0]
    return Tumblr(id=id,
                  auth_entity=auth_entity.key,
                  domains=domains,
                  domain_urls=urls,
                  name=auth_entity.user_display_name(),
                  picture=TUMBLR_AVATAR_URL % id,
                  superfeedr_secret=util.generate_secret(),
                  **kwargs)

  @staticmethod
  def _urls_and_domains(auth_entity, blog_name=None):
    """Returns this blog's URL and domain.

    Args:
      auth_entity: :class:`oauth_dropins.tumblr.TumblrAuth`
      blog_name: which blog. optional. matches the 'name' field for one of the
        blogs in auth_entity.user_json['user']['blogs'].

    Returns:
      ([string url], [string domain])
    """
    for blog in json_loads(auth_entity.user_json).get('user', {}).get('blogs', []):
      if ((blog_name and blog_name == blog.get('name')) or
          (not blog_name and blog.get('primary'))):
        return [blog['url']], [util.domain_from_link(blog['url']).lower()]

    return [], []

  def verified(self):
    """Returns True if we've found the webmention endpoint and Disqus."""
    return self.webmention_endpoint and self.disqus_shortname

  def verify(self):
    """Checks that Disqus is installed as well as the webmention endpoint.

    Stores the result in webmention_endpoint.
    """
    if self.verified():
      return

    super(Tumblr, self).verify(force=True)

    html = getattr(self, '_fetched_html', None)  # set by Source.verify()
    if not self.disqus_shortname and html:
      self.discover_disqus_shortname(html)

  def discover_disqus_shortname(self, html):
    # scrape the disqus shortname out of the page
    logging.info("Looking for Disqus shortname in fetched HTML")
    for regex in DISQUS_SHORTNAME_RES:
      match = regex.search(html)
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

    Returns:
      JSON response dict with 'id' and other fields
    """
    if not self.disqus_shortname:
      resp = util.requests_get(post_url)
      resp.raise_for_status()
      self.discover_disqus_shortname(resp.text)
      if not self.disqus_shortname:
        raise exc.HTTPBadRequest("Your Bridgy account isn't fully set up yet: "
                                 "we haven't found your Disqus account.")

    # strip slug, query and fragment from post url
    parsed = urllib.parse.urlparse(post_url)
    path = parsed.path.split('/')
    if not util.is_int(path[-1]):
      path.pop(-1)
    post_url = urllib.parse.urlunparse(parsed[:2] + ('/'.join(path), '', '', ''))

    # get the disqus thread id. details on thread queries:
    # http://stackoverflow.com/questions/4549282/disqus-api-adding-comment
    # https://disqus.com/api/docs/threads/details/
    resp = self.disqus_call(util.requests_get, DISQUS_API_THREAD_DETAILS_URL,
                            {'forum': self.disqus_shortname,
                             # ident:[tumblr_post_id] should work, but doesn't :/
                             'thread': 'link:%s' % post_url,
                             })
    thread_id = resp['id']

    # create the comment
    message = '<a href="%s">%s</a>: %s' % (author_url, author_name, content)
    resp = self.disqus_call(util.requests_post, DISQUS_API_CREATE_POST_URL,
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

    Returns:
      dict, JSON response
    """
    logging.info('Calling Disqus %s with %s', url.split('/')[-2:], params)
    params.update({
        'api_key': appengine_config.DISQUS_API_KEY,
        'api_secret': appengine_config.DISQUS_API_SECRET,
        'access_token': appengine_config.DISQUS_ACCESS_TOKEN,
        })
    kwargs.setdefault('headers', {}).update(util.REQUEST_HEADERS)
    resp = method(url, params=params, **kwargs)
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
                for b in json_loads(auth_entity.user_json)['user']['blogs']
                if b.get('name') and b.get('url')],
      }
    logging.info('Rendering choose_blog.html with %s', vars)

    self.response.headers['Content-Type'] = 'text/html'
    self.response.out.write(JINJA_ENV.get_template('choose_blog.html').render(**vars))


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
    # Tumblr doesn't seem to use scope
    # http://www.tumblr.com/docs/en/api/v2#oauth
    ('/tumblr/start', util.oauth_starter(oauth_tumblr.StartHandler).to(
      '/tumblr/choose_blog')),
    ('/tumblr/choose_blog', ChooseBlog),
    ('/tumblr/add', AddTumblr),
    ('/tumblr/delete/finish', oauth_tumblr.CallbackHandler.to('/delete/finish')),
    ('/tumblr/notify/(.+)', SuperfeedrNotifyHandler),
    ], debug=appengine_config.DEBUG)
