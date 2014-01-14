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
import models
import twitter
import util
import webapp2
from webutil import handlers

from google.appengine.ext import db


SOURCES = {cls.SHORT_NAME: cls for cls in
           (facebook.FacebookPage,
            googleplus.GooglePlusPage,
            instagram.Instagram,
            twitter.Twitter)}

TEMPLATE = """\
<!DOCTYPE html>
<html>
<head>
<link rel="canonical" href="%s" />
<style type="text/css">
.u-uid { display: none; }
</style>
</head>
%s
</html>
"""

class ItemHandler(webapp2.RequestHandler):
  """Fetches a post, repost, like, or comment and serves it as mf2 HTML or JSON.
  """
  handle_exception = handlers.handle_exception
  source = None

  def get_item(source, id):
    """Fetches and returns an object from the given source.

    To be implemented by subclasses.

    Args:
      source: bridgy.Source subclass
      id: string

    Returns: ActivityStreams object dict
    """
    raise NotImplementedError()

  def get(self, type, source_short_name, key_name, *ids):
    label = '%s:%s %s %s' % (source_short_name, key_name, type, ids)
    logging.info('Fetching %s', label)

    source_cls = SOURCES.get(source_short_name)
    if not source_cls:
      self.abort(400, "Source type '%s' not found. Known sources: %s" %
                 (source_short_name, SOURCES))

    key = db.Key.from_path(source_cls.kind(), key_name)
    self.source = db.get(key)
    if not self.source:
      self.abort(400, '%s not found' % key.to_path())

    format = self.request.get('format', 'html')
    if format not in ('html', 'json'):
      self.abort(400, 'Invalid format %s, expected html or json' % format)

    obj = self.get_item(*ids)
    if not obj:
      self.abort(404, label)

    # use https for profile pictures so we don't cause SSL mixed mode errors
    # when serving over https.
    image = obj.get('author', {}).get('image', {})
    url = image.get('url')
    if url:
      image['url'] = util.update_scheme(url, self)

    self.response.headers['Access-Control-Allow-Origin'] = '*'
    if format == 'html':
      self.response.headers['Content-Type'] = 'text/html'
      self.response.out.write(TEMPLATE % (obj.get('url', ''),
                                          microformats2.object_to_html(obj)))
    elif format == 'json':
      self.response.headers['Content-Type'] = 'application/json'
      self.response.out.write(json.dumps(microformats2.object_to_json(obj),
                                         indent=2))

  def add_original_post_urls(self, post_id, obj, prop):
    """Extracts original post URLs and adds them to an object, in place.

    Args:
      post_id: string post id
      obj: ActivityStreams post object
      prop: string property name in obj to add the original post URLs to
    """
    post = None
    try:
      post = self.source.get_post(post_id)
    except:
      logging.exception('Error fetching source post %s', post_id)
      return
    if not post:
      logging.warning('Source post %s not found', post_id)
      return

    self.source.as_source.original_post_discovery(post)

    if prop not in obj:
      obj[prop] = []
    elif not isinstance(obj[prop], list):
      obj[prop] = [obj[prop]]
    obj[prop] += [tag for tag in post['object'].get('tags', [])
                  if 'url' in tag and tag['objectType'] == 'article']

    resolved_urls = []
    for url_obj in obj[prop]:
      url = url_obj.get('url')
      if url and not util.in_webmention_blacklist(url):
        # when debugging locally, replace my (snarfed.org) URLs with localhost
        if appengine_config.DEBUG:
          if url.startswith('http://snarfed.org/'):
            url_obj['url'] = url = url.replace('http://snarfed.org/',
                                               'http://localhost/')
        # follow redirects. add resolved URLs instead of replacing them because
        # resolving may have failed during poll, in which case the webmention
        # target is checking for the shorted URL, not the resolved one.
        resolved = util.follow_redirects(url)
        if resolved != url:
          logging.debug('Resolved %s to %s', url, resolved)
          resolved_urls.append(resolved)

    obj[prop] += [{'url': url, 'objectType': 'article'} for url in resolved_urls]

    post_urls = ', '.join(o.get('url', '[none]') for o in obj[prop])
    logging.info('Original post discovery filled in %s URLs: %s', prop, post_urls)


class PostHandler(ItemHandler):
  def get_item(self, id):
    activity = self.source.get_post(id)
    return activity['object'] if activity else None


class CommentHandler(ItemHandler):
  def get_item(self, post_id, id):
    cmt = self.source.get_comment(id, activity_id=post_id)
    if not cmt:
      return None
    self.add_original_post_urls(post_id, cmt, 'inReplyTo')
    return cmt


class LikeHandler(ItemHandler):
  def get_item(self, post_id, user_id):
    like = self.source.get_like(self.source.key().name(), post_id, user_id)
    if not like:
      return None
    self.add_original_post_urls(post_id, like, 'object')
    return like


class RepostHandler(ItemHandler):
  def get_item(self, post_id, share_id):
    repost = self.source.get_share(self.source.key().name(), post_id, share_id)
    if not repost:
      return None
    self.add_original_post_urls(post_id, repost, 'object')
    return repost


application = webapp2.WSGIApplication([
    ('/(post)/(.+)/(.+)/(.+)', PostHandler),
    ('/(comment)/(.+)/(.+)/(.+)/(.+)', CommentHandler),
    ('/(like)/(.+)/(.+)/(.+)/(.+)', LikeHandler),
    ('/(repost)/(.+)/(.+)/(.+)/(.+)', RepostHandler),
    ], debug=appengine_config.DEBUG)
