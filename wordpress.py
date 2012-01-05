"""WordPress API code and datastore model classes.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']


import logging
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
# note that . does *not* match a newline by default.
POST_ID_RE = '<body class=.*(postid|page-id)-([0-9]+)'


def get_post_id(url):
  """Finds and returns the WordPress post or page id for a given URL.

  TODO: pages
  TODO: error handling
  """

  # find the wordpress post id
  resp = urlfetch.fetch(url)
  return int(re.search(POST_ID_RE, resp.content).group(2))

  
class WordPressSite(models.Destination):
  """A wordpress blog.

  key_name: '[xmlrpc url]_[blog id]', e.g. 'http://my/xmlrpc_0'

  Attributes (in addition to the properties):
    xmlrpc_url: string
    blog_id: integer
  """

  TYPE_NAME = 'WordPress'
  KEY_NAME_RE = re.compile('^(.+)_([0-9]+)$')

  username = db.StringProperty()
  password = db.StringProperty()

  def __init__(self, *args, **kwargs):
    super(WordPressSite, self).__init__(*args, **kwargs)
    self.xmlrpc_url, self.blog_id = self.KEY_NAME_RE.match(self.key().name()).groups()
    self.blog_id = int(self.blog_id)

  def display_name(self):
    """TODO: get this from the site itself."""
    return self.url

  def type_display_name(self):
    return self.TYPE_NAME

  @staticmethod
  def new(properties, handler):
    """Creates and saves a WordPressSite for the logged in user.

    Args:
      properties: dict
      handler: the current webapp.RequestHandler

    Returns: WordPressSite
    """
    properties = dict(properties)

    blog_id = properties.get('blog_id')
    if blog_id:
      blog_id = int(blog_id)
    else:
      blog_id = 0
    properties['blog_id'] = blog_id

    key_name = '%s_%d' % (properties.get('xmlrpc_url'), blog_id)
    existing = WordPressSite.get_by_key_name(key_name)
    site = WordPressSite(key_name=key_name,
                         owner=models.User.get_current_user(),
                         **properties)

    if existing:
      logging.warning('Overwriting WordPressSite %s! Old version:\n%s' %
                      (key_name, site.to_xml()))
      handler.messages.append('Updated existing %s site: %s' %
                              (existing.type_display_name(), existing.display_name()))
    else:
      handler.messages.append('Added %s site: %s' %
                              (site.type_display_name(), site.display_name()))

    # TODO: ugh, *all* of this should be transactional
    site.save()
    return site

  def add_comment(self, comment):
    """Posts a comment to this site.

    Args:
      comment: Comment instance
    """
    wp = WordPress(self.xmlrpc_url, self.blog_id, self.username, self.password)
    # i originally used a <br /> here, but xmlrpc.newComment strips it. :/ <p>
    # works though.
    content = '<i><a href="%s">On %s</a>:</i> %s' % (
      comment.source_post_url, comment.source.type_display_name(), comment.content)

    author_url = str(comment.author_url) # xmlrpclib complains about string subclasses
    post_id = get_post_id(comment.dest_post_url)
    wp.new_comment(post_id, comment.author_name, author_url, content)


class WordPress(object):
  """An XML-RPC interface to a WordPress blog.

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
                                       transport=WordPress.transport)
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

    Details: http://codex.wordpress.org/XML-RPC_wp#wp.newComment
    """
    # *don't* pass in username and password. if you do, that wordpress user's
    # name and url override the ones we provide in the xmlrpc call.
    #
    # also, use '' instead of None, even though we use allow_none=True. it
    # converts None to <nil />, which wordpress's xmlrpc server interprets as
    # "no parameter" instead of "blank parameter."
    # 
    # note that this requires anonymous commenting to be turned on in wordpress
    # via the xmlrpc_allow_anonymous_comments filter.
    return self.proxy.wp.newComment(
      self.blog_id, '', '', post_id,
      # self.blog_id, self.username, self.password, post_id,
      {'author': author, 'author_url': author_url, 'content': content})

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

    Details: http://codex.wordpress.org/XML-RPC_wp#wp.deleteComment
    """
    return self.proxy.wp.deleteComment(self.blog_id, self.username,
                                       self.password, comment_id)


# TODO: unify with facebook, etc?
class AddWordPressSite(util.Handler):
  def post(self):
    site = WordPressSite.new(self.request.params, self)
    self.redirect('/?msg=Added %s destination: %s' % (site.type_display_name(),
                                                      site.display_name()))


class DeleteWordPressSite(util.Handler):
  def post(self):
    site = WordPressSite.get_by_key_name(self.request.params['name'])
    # TODO: remove tasks, etc.
    msg = 'Deleted %s destination: %s' % (site.type_display_name(),
                                                site.display_name())
    site.delete()
    self.redirect('/?msg=' + msg)


class Go(util.Handler):
  def get(self):
    # # test get_comments()
    # wp = WordPress('http://localhost/w/xmlrpc.php', 0, '', '')
    # self.response.headers['Content-Type'] = 'text/plain'
    # self.response.out.write(`wp.get_comments(670)`)

    # # test add_comment()
    # import facebook
    # fbpage = facebook.FacebookPage(key_name='fbpage')
    # site = WordPressSite.all().get()
    # comment = models.Comment(key_name='my_comment',
    #                          source=fbpage,
    #                          dest=site,
    #                          source_post_url='http://source.com/',
    #                          # dest_post_url='http://localhost/about',
    #                          dest_post_url='http://localhost/about',
    #                          author_name='ryan',
    #                          author_url='http://snarfed.org',
    #                          content='foo bar tommy')
    # site.add_comment(comment)

    # return wp.proxy.wp.editComment(wp.blog_id, wp.username, wp.password, 26662,
    #                                {})
    # return wp.proxy.wp.getComment(wp.blog_id, wp.username, wp.password, 99999)
    # return wp.proxy.wp.deletePage(wp.blog_id, wp.username, wp.password, 999)

    # self.response.out.write(wp.delete_comment(26674))
    # self.response.out.write(wp.new_comment(670, 'name', 'http://name/', 'foo/nbar'))
    # self.response.out.write(wp.get_comments(670))
    # self.response.out.write(get_post_id('http://localhost/lists'))
    pass
    

application = webapp.WSGIApplication([
    ('/wordpress/add', AddWordPressSite),
    ('/wordpress/delete', DeleteWordPressSite),
    ('/wordpress/go', Go),
    ], debug=appengine_config.DEBUG)

def main():
  run_wsgi_app(application)


if __name__ == '__main__':
  main()
