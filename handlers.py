"""Common handlers, e.g. post and comment permalinks.

URL path format is /(post|comment)/SITE/USER_ID/OBJECT_ID, e.g.:
  /post/facebook/212038/10100823411094363
  /comment/twitter/snarfed_org/10100823411094363
"""

import json
import logging
import urlparse

from activitystreams import microformats2
import appengine_config
import facebook
import googleplus
import instagram
import twitter
import util
import webapp2
from webutil import handlers
from webutil import util

from google.appengine.ext import db


SOURCES = {cls.SHORT_NAME: cls for cls in
           (facebook.FacebookPage,
            googleplus.GooglePlusPage,
            instagram.Instagram,
            twitter.Twitter)}


class ItemHandler(webapp2.RequestHandler):
  """Fetches a post or comment and serves it as microformat2 HTML or JSON.
  """
  handle_exception = handlers.handle_exception

  def get_item(source, id):
    """Fetches and returns a post or comment from the given source.

    To be implemented by subclasses.

    Args:
      source: bridgy.Source subclass
      id: string

    Returns: ActivityStreams object dict
    """
    raise NotImplementedError()

  def get(self, source_short_name, key_name, id):
    logging.info('Fetching %s:%s object %s', source_short_name, key_name, id)

    source_cls = SOURCES.get(source_short_name, '')
    key = db.Key.from_path(source_cls.kind(), key_name)
    source = db.get(key)
    if not source:
      self.abort(400, '%s not found' % key.to_path())

    format = self.request.get('format', 'html')
    if format not in ('html', 'json'):
      self.abort(400, 'Invalid format %s, expected html or json' % format)

    obj = self.get_item(source, id)
    if format not in ('html', 'json'):
      self.abort(404, 'Object %s not found' % id)

    self.response.headers['Access-Control-Allow-Origin'] = '*'
    if format == 'html':
      self.response.headers['Content-Type'] = 'text/html'
      self.response.out.write("""\
<!DOCTYPE html>
<html>
%s
</html>
""" % microformats2.object_to_html(obj))
    elif format == 'json':
      self.response.headers['Content-Type'] = 'application/json'
      self.response.out.write(json.dumps(microformats2.object_to_json(obj),
                                         indent=2))


class PostHandler(ItemHandler):
  def get_item(self, source, id):
    activity = source.get_post(id)
    return activity['object'] if activity else None


class CommentHandler(ItemHandler):
  def get_item(self, source, id):
    cmt = source.get_comment(id)
    if not cmt:
      return None

    # fetch the post, perform original post discovery on it, and add the
    # resulting links to the comment's inReplyTo.
    #
    # TODO: for twitter and other sites with threaded comments, the comment's
    # inReplyTo won't point to the very first (original) post of the thread.
    # figure out how to handle that, ideally without having to walk back each
    # step in the thread.
    post_id = None
    for in_reply_to in cmt.get('inReplyTo', []):
      if 'id' in in_reply_to:
        domain, post_id = util.parse_tag_uri(in_reply_to['id'])
        if domain == source.as_source.DOMAIN:
          break

    if not post_id:
      logging.warning('Could not find source post in inReplyTo!')
      return cmt

    post = source.get_post(post_id)
    if not post:
      logging.warning('Could not fetch source post %s', post_id)
      return cmt

    source.as_source.original_post_discovery(post)
    cmt['inReplyTo'] += [tag for tag in post['object'].get('tags', [])
                         if 'url' in tag and tag['objectType'] == 'article']
    logging.info('Original post discovery filled in inReplyTo URLs: %s',
                 ', '.join(obj.get('url') for obj in cmt['inReplyTo']))

    return cmt


application = webapp2.WSGIApplication([
    ('/post/([^/]+)/([^/]+)/([^/]+)', PostHandler),
    ('/comment/([^/]+)/([^/]+)/([^/]+)', CommentHandler),
    ], debug=appengine_config.DEBUG)
