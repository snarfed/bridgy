"""WordPress REST API (including WordPress.com) hosted blog implementation.

https://developer.wordpress.com/docs/api/
create returns id, can lookup by id

disqus for tumblr
http://disqus.com/api/docs/
http://disqus.com/api/docs/posts/create/
https://github.com/disqus/DISQUS-API-Recipes/blob/master/snippets/php/create-guest-comment.php
http://help.disqus.com/customer/portal/articles/466253-what-html-tags-are-allowed-within-comments-
create returns id, can lookup by id w/getContext?

test:
curl localhost:8080/webmention/wordpress \
  -d 'source=http://localhost/response.html&target=http://ryandc.wordpress.com/2013/03/24/mac-os-x/'


blogger
https://developers.google.com/blogger/docs/2.0/developers_guide_protocol
https://support.google.com/blogger/answer/42064?hl=en
create comment:
https://developers.google.com/blogger/docs/2.0/developers_guide_protocol#CreatingComments



superfeedr:
https://superfeedr.com/users/snarfed
http://documentation.superfeedr.com/subscribers.html
http://documentation.superfeedr.com/schema.html
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import collections
import datetime
import json
import logging
import os
import urllib
import urlparse

import appengine_config
from appengine_config import HTTP_TIMEOUT

from activitystreams.oauth_dropins import wordpress_rest as oauth_wordpress
import models
import util

from google.appengine.ext import ndb
import webapp2

API_CREATE_COMMENT_URL = 'https://public-api.wordpress.com/rest/v1/sites/%s/posts/%d/replies/new?pretty=true'
API_POST_SLUG_URL = 'https://public-api.wordpress.com/rest/v1/sites/%s/posts/slug:%s?pretty=true'

FakeAsClass = collections.namedtuple('FakeAsClass', ('NAME',))


class WordPress(models.Source):
  """A WordPress blog.

  The key name is the base URL.
  """
  AS_CLASS = FakeAsClass(NAME='WordPress.com')
  SHORT_NAME = 'wordpress'

  @staticmethod
  def new(handler, auth_entity=None, **kwargs):
    """Creates and returns a WordPress for the logged in user.

    Args:
      handler: the current RequestHandler
      auth_entity: oauth_dropins.wordpress.WordPressAuth
    """
    return WordPress(id=auth_entity.key.id(),
                     auth_entity=auth_entity.key,
                     url=auth_entity.blog_url,
                     name=auth_entity.user_display_name(),
                     **kwargs)

  def _url_and_domain(self, auth_entity):
    """Returns this blog's URL and domain.

    Args:
      auth_entity: oauth_dropins.wordpress_rest.WordPressAuth

    Returns: (string url, string domain, True)
    """
    return auth_entity.blog_url, auth_entity.key.id(), True


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
    auth_entity = self.auth_entity.get()

    # extract the post's slug and look up its post id
    path = urlparse.urlparse(post_url).path
    if path.endswith('/'):
      path = path[:-1]
    slug = path.split('/')[-1]
    try:
      post_id = int(slug)
    except ValueError:
      url = API_POST_SLUG_URL % (auth_entity.key.id(), slug)
      resp = auth_entity.urlopen(url).read()
      post_id = json.loads(resp)['ID']

    # create the comment
    url = API_CREATE_COMMENT_URL % (auth_entity.key.id(), post_id)
    data = {'content': '<a href="%s">%s</a>: %s\n\n<a href="">via foo</a>' %
            (author_url, author_name, content)}
    resp = auth_entity.urlopen(url, data=urllib.urlencode(data)).read()
    resp = json.loads(resp)
    resp['id'] = resp.pop('ID', None)
    return resp


class AddWordPress(oauth_wordpress.CallbackHandler, util.Handler):
  def finish(self, auth_entity, state=None):
    self.maybe_add_or_delete_source(WordPress, auth_entity, state)


application = webapp2.WSGIApplication([
    # OAuth scopes are set in listen.html and publish.html
    ('/wordpress/start', oauth_wordpress.StartHandler.to('/wordpress/add')),
    ('/wordpress/add', AddWordPress),
    ('/wordpress/delete/start', oauth_wordpress.CallbackHandler.to('/delete/finish')),
    ], debug=appengine_config.DEBUG)
