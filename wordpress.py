"""Wordpress API code and datastore model classes.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']


import re
import traceback
import xmlrpclib

import appengine_config
import models
import util

from google.appengine.api import urlfetch
from google.appengine.ext import db
from google.appengine.ext import webapp
from google.appengine.ext.webapp.util import run_wsgi_app

# TODO: this isn't global. see e.g.
# http://wordpress.org/news/2011/07/wordpress-3-2-1/
#
# from rss feed:
# <link>http://wordpress.org/news/2011/07/wordpress-3-2-1/</link>
# ...
# <guid isPermaLink="false">http://wordpress.org/news/?p=1982</guid>
#
POST_ID_RE = "<link rel='shortlink' href='[^?]+\?p=([0-9]+)' />"


def get_post_id(url):
  """Finds and returns the Wordpress post or page id for a given URL.

  TODO: pages
  TODO: error handling
  """

  # find the wordpress post id
  resp = urlfetch.fetch(url)
  return re.search(POST_ID_RE, resp.content).group(1)

  
class WordpressSite(models.Destination):
  """A wordpress blog.

  key_name: '[xmlrpc url]_[blog id]', e.g. 'http://my/xmlrpc_0'

  Attributes (in addition to the properties):
    xmlrpc_url: string
    blog_id: integer
  """

  TYPE_NAME = 'Wordpress'
  KEY_NAME_RE = re.compile('^(.+)_([0-9]+)$')

  username = db.StringProperty()
  password = db.StringProperty()
  # post_prefix_url = db.LinkProperty(required=True)

  def __init__(self, *args, **kwargs):
    super(WordpressSite, self).__init__(*args, **kwargs)
    self.xmlrpc_url, self.blog_id = self.KEY_NAME_RE.match(self.key().name()).groups()
    self.blog_id = int(self.blog_id)

  def display_name(self):
    """TODO: get this from the site itself."""
    return self.xmlrpc_url

  def type_display_name(self):
    return self.TYPE_NAME

  @staticmethod
  def new(properties, handler):
    """Creates and saves a WordpressSite for the logged in user.

    Args:
      properties: dict
      handler: the current webapp.RequestHandler

    Returns: WordpressSite
    """
    properties = dict(properties)

    blog_id = properties.get('blog_id')
    if blog_id:
      blog_id = int(blog_id)
    else:
      blog_id = 0
    properties['blog_id'] = blog_id

    key_name = '%s_%d' % (properties.get('xmlrpc_url'), blog_id)
    existing = WordpressSite.get_by_key_name(key_name)
    site = WordpressSite(key_name=key_name, **properties)

    if existing:
      logging.warning('Overwriting WordpressSite %s! Old version:\n%s' %
                      (key_name, site.to_xml()))
      handler.messages.append('Updated existing %s site: %s' %
                              (existing.type_display_name(), existing.display_name()))
    else:
      handler.messages.append('Added %s site: %s' %
                              (site.type_display_name(), site.display_name()))

    # TODO: ugh, *all* of this should be transactional
    site.save()
    models.User.get_current_user().add_dest(site)
    return site

  def add_comment(self, comment):
    """Posts a comment to this site.

    Args:
      comment: Comment instance
    """
    wp = Wordpress(self.xmlrpc_url, self.blog_id, self.username, self.password)
    content = '%s\n<a href="%s">(from %s)</a>' % (
      comment.content, comment.source_post_url, comment.source.type_display_name())

    author_url = str(comment.author_url) # xmlrpclib complains about string subclasses
    wp.new_comment(get_post_id(comment.dest_post_url), comment.author_name,
                   author_url, content)


class Wordpress(object):
  """An XML-RPC interface to a Wordpress blog.

  Class attributes:
    transport: Transport instance passed to ServerProxy()

  Attributes:
    proxy: xmlrpclib.ServerProxy
    blog_id: integer
    username: string, username for authentication, may be None
    password: string, username for authentication, may be None
  """

  transport = None

  def __init__(self, xmlrpc_url, blog_id, username, password):
    self.proxy = xmlrpclib.ServerProxy(xmlrpc_url, allow_none=True,
                                       transport=Wordpress.transport)
    self.blog_id = blog_id
    self.username = username
    self.password = password

  def get_comments(self, post_id):
    """Fetches all of the comments for a given post or page.

    TODO: error handling
  
    Args:
      post_id: integer, post or page id

    Returns: list dict with these keys, all strings except dateCreated which is
    datetime in GMT:
      dateCreated, user_id, comment_id, parent, status, content, link, post_id,
      post_title, author, author_url, author_email, author_ip

    Details: http://codex.wordpress.org/XML-RPC_wp#wp.getComments
    """
    return self.proxy.wp.getComments(self.blog_id, self.username, self.password,
                                     {'post_id': post_id})
  
  def new_comment(self, post_id, author, author_url, content):
    """Adds a new comment.

    TODO: error handling
  
    Args:
      post_id: integer, post or page id
      author: string, human-readable name
      author_url: string
      content: string

    Returns: integer, the comment id
    """
    return self.proxy.wp.newComment(self.blog_id, self.username, self.password,
                                    {'post_id': post_id, 'author': author,
                                     'author_url': author_url, 'content': content})

  def delete_comment(self, comment_id):
    """Deletes a comment.

    Note: if the comment doesn't exist, this raises a xmlprclib.Fault:
    Fault: <Fault 403: 'You are not allowed to moderate comments on this site.'>
    this is a wordpress bug that's fixed in head:
    http://core.trac.wordpress.org/ticket/18104

    TODO: error handling
  
    Args:
      comment_id: integer, comment id

    Returns: boolean, whether the delete succeeded
    """
    return self.proxy.wp.deleteComment(self.blog_id, self.username, self.password,
                                       {'comment_id': comment_id})


# TODO: unify with facebook, etc?
class AddWordpressSite(util.Handler):
  def post(self):
    site = WordpressSite.new(self.request.params, self)
    self.redirect('/?msg=Added %s destination: %s' % (site.type_display_name(),
                                                      site.display_name()))


class DeleteWordpressSite(util.Handler):
  def post(self):
    site = WordpressSite.get_by_key_name(self.request.params['name'])
    # TODO: remove tasks, etc.
    msg = 'Deleted %s destination: %s' % (site.type_display_name(),
                                                site.display_name())
    site.delete()
    self.redirect('/?msg=' + msg)


class Go(util.Handler):
  def get(self):
    wp = Wordpress('http://localhost/w/xmlrpc.php', 0, 'ryan', 'w1JJmcwzD$T')
    self.response.headers['Content-Type'] = 'text/plain'
    self.response.out.write(`wp.get_comments(670)`)
    # return wp.proxy.wp.editComment(wp.blog_id, wp.username, wp.password, 26662,
    #                                {})
    # return wp.proxy.wp.getComment(wp.blog_id, wp.username, wp.password, 99999)
    # return wp.proxy.wp.deletePage(wp.blog_id, wp.username, wp.password, 999)

    # self.response.out.write(wp.delete_comment(26674))
    # self.response.out.write(wp.new_comment(670, 'name', 'http://name/', 'foo/nbar'))
    # self.response.out.write(wp.get_comments(670))
    # self.response.out.write(get_post_id('http://localhost/lists'))
    

application = webapp.WSGIApplication([
    ('/wordpress/add', AddWordpressSite),
    ('/wordpress/delete', DeleteWordpressSite),
    ('/wordpress/go', Go),
    ], debug=appengine_config.DEBUG)

def main():
  run_wsgi_app(application)


if __name__ == '__main__':
  main()
