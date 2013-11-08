"""WordPress API code and datastore model classes.

Note that WordPress doesn't support specifying a date for new comments!
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']


import logging
import re
import xmlrpclib

import appengine_config
import models
import util

from google.appengine.api import urlfetch
from google.appengine.ext import db
import webapp2

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

  @staticmethod
  def new(handler):
    """Creates and saves a WordPressSite for the logged in user.

    Args:
      handler: the current RequestHandler

    Returns: WordPressSite

    Raises: BadValueError if url or xmlrpc_url are bad
    """
    properties = dict(handler.request.params)
    for prop in 'url', 'xmlrpc_url':
      db.LinkProperty().validate(properties.get(prop))

    blog_id = properties.get('blog_id')
    if blog_id:
      blog_id = int(blog_id)
    else:
      blog_id = 0
    properties['blog_id'] = blog_id

    key_name = '%s_%d' % (properties['xmlrpc_url'], blog_id)
    return WordPressSite(key_name=key_name,
                         owner=models.User.get_current_user(),
                         **properties)

  def add_comment(self, comment):
    """Posts a comment to this site.

    Args:
      comment: Comment instance
    """
    wp = WordPress(self.xmlrpc_url, self.blog_id, self.username, self.password)

    # note that wordpress strips many html tags (e.g. br) and almost all
    # attributes (e.g. class) from html tags in comment contents. so, convert
    # some of those tags to other tags that wordpress accepts.
    content = re.sub('<br */?>', '<p />', comment.content)

    # since available tags are limited (see above), i use a fairly unique tag
    # for the "via ..." link - cite - that site owners can use to style.
    #
    # example css on my site:
    #
    # .comment-content cite a {
    #     font-size: small;
    #     color: gray;
    # }
    content = '%s <cite><a href="%s">via %s</a></cite>' % (
      content, comment.source_post_url, comment.source.type_display_name())

    author_url = str(comment.author_url) # xmlrpclib complains about string subclasses
    post_id = get_post_id(comment.dest_post_url)

    try:
      wp.new_comment(post_id, comment.author_name, author_url, content)
    except xmlrpclib.Fault, e:
      # if it's a dupe, we're done!
      if (e.faultCode == 500 and
          e.faultString.startswith('Duplicate comment detected')):
        pass
      else:
        raise


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
    try:
      WordPressSite.create_new(self)
    except db.BadValueError, e:
      self.messages.append(str(e))
    self.redirect('/')

class DeleteWordPressSite(util.Handler):
  def post(self):
    site = WordPressSite.get_by_key_name(self.request.params['name'])
    # TODO: remove tasks, etc.
    msg = 'Deleted %s destination: %s' % (site.type_display_name(),
                                                site.display_name())
    site.delete()
    self.redirect('/?msg=' + msg)


application = webapp2.WSGIApplication([
    ('/wordpress/add', AddWordPressSite),
    ('/wordpress/delete', DeleteWordPressSite),
    ], debug=appengine_config.DEBUG)
