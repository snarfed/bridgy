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
import re
import string
import urlparse

import appengine_config

from activitystreams import microformats2
from activitystreams.oauth_dropins.webutil import handlers
import original_post_discovery
import facebook
import googleplus
import instagram
import models
import twitter
import util
import webapp2
import wordpress_rest

from google.appengine.ext import ndb


SOURCES = {cls.SHORT_NAME: cls for cls in
           (facebook.FacebookPage,
            googleplus.GooglePlusPage,
            instagram.Instagram,
            twitter.Twitter,
            wordpress_rest.WordPress,
            )}

TEMPLATE = string.Template("""\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>$title</title>
<link rel="canonical" href="$url" />
<style type="text/css">
.u-uid { display: none; }
</style>
</head>
$body
</html>
""")

class ItemHandler(webapp2.RequestHandler):
  """Fetches a post, repost, like, or comment and serves it as mf2 HTML or JSON.
  """
  handle_exception = handlers.handle_exception
  source = None

  VALID_ID = re.compile(r'^[\w.+:@-]+$')

  def head(self, *args):
    """Return an empty 200 with no caching directives."""

  def get_item(source, id):
    """Fetches and returns an object from the given source.

    To be implemented by subclasses.

    Args:
      source: bridgy.Source subclass
      id: string

    Returns: ActivityStreams object dict
    """
    raise NotImplementedError()

  def get(self, type, source_short_name, string_id, *ids):
    source_cls = SOURCES.get(source_short_name)
    if not source_cls:
      self.abort(400, "Source type '%s' not found. Known sources: %s" %
                 (source_short_name, SOURCES))

    self.source = source_cls.get_by_id(string_id)
    if not self.source:
      self.abort(400, '%s %s not found' % (source_short_name, string_id))

    format = self.request.get('format', 'html')
    if format not in ('html', 'json'):
      self.abort(400, 'Invalid format %s, expected html or json' % format)

    for id in ids:
      if not self.VALID_ID.match(id):
        self.abort(404, 'Non-numeric id %s' % id)

    label = '%s:%s %s %s' % (source_short_name, string_id, type, ids)
    logging.info('Fetching %s', label)
    obj = self.get_item(*ids)
    if not obj:
      self.abort(404, label)

    # use https for profile pictures so we don't cause SSL mixed mode errors
    # when serving over https.
    author = obj.get('author', {})
    image = author.get('image', {})
    url = image.get('url')
    if url:
      image['url'] = util.update_scheme(url, self)

    self.response.headers['Access-Control-Allow-Origin'] = '*'
    if format == 'html':
      self.response.headers['Content-Type'] = 'text/html; charset=utf-8'
      self.response.out.write(TEMPLATE.substitute({
            'url': obj.get('url', ''),
            'body': microformats2.object_to_html(obj),
            'title': obj.get('title', obj.get('content', 'Bridgy Response')),
            }))
    elif format == 'json':
      self.response.headers['Content-Type'] = 'application/json; charset=utf-8'
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

    original_post_discovery.discover(self.source, post, fetch_hfeed=False)

    if prop not in obj:
      obj[prop] = []
    elif not isinstance(obj[prop], list):
      obj[prop] = [obj[prop]]
    obj[prop] += [tag for tag in post['object'].get('tags', [])
                  if 'url' in tag and tag['objectType'] == 'article']

    resolved_urls = set()
    for url_obj in obj[prop]:
      url = url_obj.get('url')
      if not url:
        continue
      # when debugging locally, replace my (snarfed.org) URLs with localhost
      if appengine_config.DEBUG:
        if url.startswith('http://snarfed.org/'):
          url_obj['url'] = url = url.replace('http://snarfed.org/',
                                             'http://localhost/')
        elif url.startswith('http://kylewm.com'):
          url_obj['url'] = url = url.replace('http://kylewm.com/',
                                             'http://localhost/')

      resolved, _, send = util.get_webmention_target(url)
      if send and resolved != url:
        resolved_urls.add(resolved)

    obj[prop] += [{'url': url, 'objectType': 'article'} for url in resolved_urls]

    post_urls = ', '.join(o.get('url', '[none]') for o in obj[prop])
    logging.info('Original post discovery filled in %s URLs: %s', prop, post_urls)


class PostHandler(ItemHandler):
  def get_item(self, id):
    activity = self.source.get_post(id)
    return activity['object'] if activity else None


class CommentHandler(ItemHandler):
  def get_item(self, post_id, id):
    cmt = self.source.get_comment(id, activity_id=post_id,
                                  activity_author_id=self.source.key.id())
    if not cmt:
      return None
    self.add_original_post_urls(post_id, cmt, 'inReplyTo')
    return cmt


class LikeHandler(ItemHandler):
  def get_item(self, post_id, user_id):
    like = self.source.get_like(self.source.key.string_id(), post_id, user_id)
    if not like:
      return None
    self.add_original_post_urls(post_id, like, 'object')
    return like


class RepostHandler(ItemHandler):
  def get_item(self, post_id, share_id):
    repost = self.source.get_share(self.source.key.string_id(), post_id, share_id)
    if not repost:
      return None
    self.add_original_post_urls(post_id, repost, 'object')
    return repost


class RsvpHandler(ItemHandler):
  def get_item(self, event_id, user_id):
    rsvp = self.source.get_rsvp(self.source.key.string_id(), event_id, user_id)
    if not rsvp:
      return None
    self.add_original_post_urls(event_id, rsvp, 'inReplyTo')
    return rsvp


application = webapp2.WSGIApplication([
    ('/(post)/(.+)/(.+)/(.+)', PostHandler),
    ('/(comment)/(.+)/(.+)/(.+)/(.+)', CommentHandler),
    ('/(like)/(.+)/(.+)/(.+)/(.+)', LikeHandler),
    ('/(repost)/(.+)/(.+)/(.+)/(.+)', RepostHandler),
    ('/(rsvp)/(.+)/(.+)/(.+)/(.+)', RsvpHandler),
    ], debug=appengine_config.DEBUG)
