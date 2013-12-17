"""Common handlers, e.g. post and comment permalinks.

URL paths are:

/post/SITE/USER_ID/POST_ID
  e.g. /post/facebook/212038/10100823411094363

/comment/SITE/USER_ID/POST_ID/COMMENT_ID
  e.g. /comment/twitter/snarfed_org/10100823411094363/999999

/like/SITE/USER_ID/POST_ID/LIKED_BY_USER_ID
  e.g. /like/twitter/snarfed_org/10100823411094363/999999

/repost/SITE/USER_ID/POST_ID/REPOSTED_BY_USER_ID
  e.g. /repost/twitter/snarfed_org/10100823411094363/999999
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
  """Fetches a post, repost, like, or comment and serves it as mf2 HTML or JSON.
  """
  handle_exception = handlers.handle_exception

  def get_item(source, id):
    """Fetches and returns an object from the given source.

    To be implemented by subclasses.

    Args:
      source: bridgy.Source subclass
      id: string

    Returns: ActivityStreams object dict
    """
    raise NotImplementedError()

  def get(self, source_short_name, key_name, *ids):
    logging.info('Fetching %s:%s object %s', source_short_name, key_name, ids)

    source_cls = SOURCES.get(source_short_name, '')
    key = db.Key.from_path(source_cls.kind(), key_name)
    source = db.get(key)
    if not source:
      self.abort(400, '%s not found' % key.to_path())

    format = self.request.get('format', 'html')
    if format not in ('html', 'json'):
      self.abort(400, 'Invalid format %s, expected html or json' % format)

    obj = self.get_item(source, *ids)
    if not obj:
      self.abort(404, 'Object %s not found' % ids)

    self.response.headers['Access-Control-Allow-Origin'] = '*'
    if format == 'html':
      self.response.headers['Content-Type'] = 'text/html'
      self.response.out.write("""\
<!DOCTYPE html>
<html>
<head><link rel="canonical" href="%s" /></head>
%s
</html>
""" % (obj.get('url', ''), microformats2.object_to_html(obj)))
    elif format == 'json':
      self.response.headers['Content-Type'] = 'application/json'
      self.response.out.write(json.dumps(microformats2.object_to_json(obj),
                                         indent=2))


class PostHandler(ItemHandler):
  def get_item(self, source, id):
    activity = source.get_post(id)
    return activity['object'] if activity else None


class CommentHandler(ItemHandler):
  def get_item(self, source, post_id, id):
    cmt = source.get_comment(id, activity_id=post_id)
    if not cmt:
      return None

    post = None
    try:
      post = source.get_post(post_id)
    except:
      logging.exception('Error fetching source post %s', post_id)
    if not post:
      logging.warning('Source post %s not found', post_id)
      return cmt

    source.as_source.original_post_discovery(post)
    in_reply_tos = cmt.setdefault('inReplyTo', [])
    in_reply_tos += [tag for tag in post['object'].get('tags', [])
                     if 'url' in tag and tag['objectType'] == 'article']

    # When debugging locally, replace my (snarfed.org) URLs with localhost
    if appengine_config.DEBUG:
      for obj in cmt['inReplyTo']:
        if obj.get('url', '').startswith('http://snarfed.org/'):
          obj['url'] = obj['url'].replace('http://snarfed.org/',
                                          'http://localhost/')

    logging.info('Original post discovery filled in inReplyTo URLs: %s',
                 ', '.join(obj.get('url', 'none') for obj in cmt['inReplyTo']))

    return cmt


class LikeHandler(ItemHandler):
  def get_item(self, source, post_id, user_id):
    return source.get_like(user_id, post_id)


class RepostHandler(ItemHandler):
  def get_item(self, source, post_id, user_id):
    return source.get_repost(user_id, post_id)


application = webapp2.WSGIApplication([
    ('/post/(.+)/(.+)/(.+)', PostHandler),
    ('/comment/(.+)/(.+)/(.+)/(.+)', CommentHandler),
    ('/like/(.+)/(.+)/(.+)/(.+)', LikeHandler),
    ('/repost/(.+)/(.+)/(.+)/(.+)', RepostHandler),
    ], debug=appengine_config.DEBUG)
