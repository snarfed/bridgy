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

/rsvp/SITE/USER_ID/EVENT_ID/RSVP_USER_ID
  e.g. /rsvp/facebook/212038/12345/67890
"""

import copy
import json
import logging
import re
import string

import appengine_config

from granary import microformats2
from granary.microformats2 import first_props
from oauth_dropins.webutil import handlers
import models
import original_post_discovery
import util
import webapp2

# Import source class files so their metaclasses are initialized.
import facebook
import flickr
import googleplus
import instagram
import twitter

TEMPLATE = string.Template("""\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>$title</title>
<style type="text/css">
body {
  font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
}
.u-uid {
  display: none;
}
.u-photo {
  max-width: 50px;
  border-radius: 4px;
}
.e-content {
  margin-top: 10px;
  font-size: 1.3em;
}
</style>
</head>
$body
</html>
""")


def listify(obj, prop):
  """Converts obj[prop] to a list if it's not already.

  If obj[prop] exists and isn't a list, puts it inside a list.
  """
  val = obj.setdefault(prop, [])
  if not isinstance(val, list):
    obj[prop] = [val]
  return obj[prop]


class ItemHandler(webapp2.RequestHandler):
  """Fetches a post, repost, like, or comment and serves it as mf2 HTML or JSON.
  """
  handle_exception = handlers.handle_exception
  source = None

  VALID_ID = re.compile(r'^[\w.+:@-]+$')

  def head(self, *args):
    """Return an empty 200 with no caching directives."""

  def get_item(self, id):
    """Fetches and returns an object from the given source.

    To be implemented by subclasses.

    Args:
      source: bridgy.Source subclass
      id: string

    Returns: ActivityStreams object dict
    """
    raise NotImplementedError()

  def get_post(self, post_id, source_fn=None):
    """Utility method fetches the original post
    Args:
      post_id: string, site-specific post id
      source_fn: optional reference to a Source method,
        defaults to Source.get_post.

    Returns: ActivityStreams object dict
    """
    try:
      post = (source_fn or self.source.get_post)(post_id)
      if not post:
        logging.warning('Source post %s not found', post_id)
      return post
    except Exception, e:
      # use interpret_http_exception to log HTTP errors
      if not util.interpret_http_exception(e)[0]:
        logging.warning(
          'Error fetching source post %s', post_id, exc_info=True)

  def get(self, type, source_short_name, string_id, *ids):
    source_cls = models.sources.get(source_short_name)
    if not source_cls:
      self.abort(400, "Source type '%s' not found. Known sources: %s" %
                 (source_short_name, filter(None, models.sources.keys())))

    self.source = source_cls.get_by_id(string_id)
    if not self.source:
      self.abort(400, '%s %s not found' % (source_short_name, string_id))

    format = self.request.get('format', 'html')
    if format not in ('html', 'json'):
      self.abort(400, 'Invalid format %s, expected html or json' % format)

    for id in ids:
      if not self.VALID_ID.match(id):
        self.abort(404, 'Invalid id %s' % id)

    label = '%s:%s %s %s' % (source_short_name, string_id, type, ids)
    logging.info('Fetching %s', label)
    try:
      obj = self.get_item(*ids)
    except Exception, e:
      # pass through all API HTTP errors if we can identify them
      code, body = util.interpret_http_exception(e)
      if code:
        self.response.status_int = int(code)
        self.response.headers['Content-Type'] = 'text/plain'
        self.response.write('%s error:\n%s' % (self.source.GR_CLASS.NAME, body))
        return
      else:
        raise

    if not obj:
      self.abort(404, label)

    # use https for profile pictures so we don't cause SSL mixed mode errors
    # when serving over https.
    author = obj.get('author', {})
    image = author.get('image', {})
    url = image.get('url')
    if url:
      image['url'] = util.update_scheme(url, self)

    mf2_json = microformats2.object_to_json(obj)

    # try to include the author's silo profile url
    author = first_props(mf2_json.get('properties', {})).get('author', {})
    author_uid = first_props(author.get('properties', {})).get('uid', '')
    if author_uid:
      parsed = util.parse_tag_uri(author_uid)
      if parsed:
        silo_url = self.source.gr_source.user_url(parsed[1])
        urls = author.get('properties', {}).setdefault('url', [])
        if silo_url not in microformats2.get_string_urls(urls):
          urls.append(silo_url)

    # write the response!
    self.response.headers['Access-Control-Allow-Origin'] = '*'
    if format == 'html':
      self.response.headers['Content-Type'] = 'text/html; charset=utf-8'
      self.response.out.write(TEMPLATE.substitute({
            'url': obj.get('url', ''),
            'body': microformats2.json_to_html(mf2_json),
            'title': obj.get('title', obj.get('content', 'Bridgy Response')),
            }))
    elif format == 'json':
      self.response.headers['Content-Type'] = 'application/json; charset=utf-8'
      self.response.out.write(json.dumps(mf2_json, indent=2))

  def merge_urls(self, obj, property, urls, object_type='article'):
    """Updates an object's ActivityStreams URL objects in place.

    Adds all URLs in urls that don't already exist in obj[property].

    ActivityStreams schema details:
    http://activitystrea.ms/specs/json/1.0/#id-comparison

    Args:
      obj: ActivityStreams object to merge URLs into
      property: string property to merge URLs into
      urls: sequence of string URLs to add
      object_type: stored as the objectType alongside each URL
    """
    existing = set(filter(None, (u.get('url') for u in listify(obj, property))))
    obj[property] += [{'url': url, 'objectType': object_type} for url in urls
                      if url not in existing]


# Note that mention links are included in posts and comments, but not
# likes, reposts, or rsvps. Matches logic in poll() (step 4) in tasks.py!
class PostHandler(ItemHandler):
  def get_item(self, id):
    post = self.source.get_post(id)
    if not post:
      return None

    originals, mentions = original_post_discovery.discover(
      self.source, post, fetch_hfeed=False)
    obj = post['object']
    obj['upstreamDuplicates'] = list(
      set(listify(obj, 'upstreamDuplicates')) | originals)
    self.merge_urls(obj, 'tags', mentions, object_type='mention')
    return obj


class CommentHandler(ItemHandler):
  def get_item(self, post_id, id):
    cmt = self.source.get_comment(id, activity_id=post_id,
                                  activity_author_id=self.source.key.id())
    if not cmt:
      return None
    post = self.get_post(post_id)
    if post:
      originals, mentions = original_post_discovery.discover(
        self.source, post, fetch_hfeed=False)
      self.merge_urls(cmt, 'inReplyTo', originals)
      self.merge_urls(cmt, 'tags', mentions, object_type='mention')
    return cmt


class LikeHandler(ItemHandler):
  def get_item(self, post_id, user_id):
    like = self.source.get_like(self.source.key.string_id(), post_id, user_id)
    if not like:
      return None
    post = self.get_post(post_id)
    if post:
      originals, mentions = original_post_discovery.discover(
        self.source, post, fetch_hfeed=False)
      self.merge_urls(like, 'object', originals)
    return like


class RepostHandler(ItemHandler):
  def get_item(self, post_id, share_id):
    repost = self.source.get_share(self.source.key.string_id(), post_id, share_id)
    if not repost:
      return None
    post = self.get_post(post_id)
    if post:
      originals, mentions = original_post_discovery.discover(
        self.source, post, fetch_hfeed=False)
      self.merge_urls(repost, 'object', originals)
    return repost


class RsvpHandler(ItemHandler):
  def get_item(self, event_id, user_id):
    rsvp = self.source.get_rsvp(self.source.key.string_id(), event_id, user_id)
    if not rsvp:
      return None
    event = self.get_post(event_id, source_fn=self.source.get_event)
    if event:
      originals, mentions = original_post_discovery.discover(
        self.source, event, fetch_hfeed=False)
      self.merge_urls(rsvp, 'inReplyTo', originals)
    return rsvp


application = webapp2.WSGIApplication([
    ('/(post)/(.+)/(.+)/(.+)', PostHandler),
    ('/(comment)/(.+)/(.+)/(.+)/(.+)', CommentHandler),
    ('/(like)/(.+)/(.+)/(.+)/(.+)', LikeHandler),
    ('/(repost)/(.+)/(.+)/(.+)/(.+)', RepostHandler),
    ('/(rsvp)/(.+)/(.+)/(.+)/(.+)', RsvpHandler),
    ], debug=appengine_config.DEBUG)
