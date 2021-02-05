"""Common handlers, e.g. post and comment permalinks.

Docs: https://brid.gy/about#source-urls

URL paths are:

/post/SITE/USER_ID/POST_ID
  e.g. /post/fflickr/212038/10100823411094363

/comment/SITE/USER_ID/POST_ID/COMMENT_ID
  e.g. /comment/twitter/snarfed_org/10100823411094363/999999

/like/SITE/USER_ID/POST_ID/LIKED_BY_USER_ID
  e.g. /like/twitter/snarfed_org/10100823411094363/999999

/repost/SITE/USER_ID/POST_ID/REPOSTED_BY_USER_ID
  e.g. /repost/twitter/snarfed_org/10100823411094363/999999

/rsvp/SITE/USER_ID/EVENT_ID/RSVP_USER_ID
  e.g. /rsvp/facebook/212038/12345/67890
"""
import logging
import re
import string

from cachetools import cachedmethod, TTLCache
from granary import microformats2
from granary.microformats2 import first_props
from oauth_dropins.webutil import handlers
from oauth_dropins.webutil.util import json_dumps, json_loads
import webapp2

import models
import original_post_discovery
import util

# Import source class files so their metaclasses are initialized.
import blogger, flickr, github, instagram, mastodon, medium, reddit, tumblr, twitter, wordpress_rest

CACHE_TIME = 60 * 15  # 15m

TEMPLATE = string.Template("""\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
$refresh
<title>$title</title>
<style type="text/css">
body {
  font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
}
.p-uid {
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


class ItemHandler(util.Handler):
  """Fetches a post, repost, like, or comment and serves it as mf2 HTML or JSON.
  """
  handle_exception = handlers.handle_exception
  source = None

  VALID_ID = re.compile(r'^[\w.+:@=<>-]+$')

  @util.canonicalize_domain
  def head(self, *args):
    """Return an empty 200 with no caching directives."""

  def get_item(self, id, **kwargs):
    """Fetches and returns an object from the given source.

    To be implemented by subclasses.

    Args:
      source: :class:`models.Source` subclass
      id: string

    Returns:
      ActivityStreams object dict
    """
    raise NotImplementedError()

  def get_title(self, obj):
    """Returns the string to be used in the <title> tag.

    Args:
      obj: ActivityStreams object
    """
    return obj.get('title') or obj.get('content') or 'Bridgy Response'

  def get_post(self, id, **kwargs):
    """Fetch a post.

    Args:
      id: string, site-specific post id
      is_event: bool
      kwargs: passed through to :meth:`get_activities`

    Returns:
      ActivityStreams object dict
    """
    try:
      posts = self.source.get_activities(
          activity_id=id, user_id=self.source.key_id(), **kwargs)
      if posts:
        return posts[0]
      logging.warning('Source post %s not found', id)
    except Exception as e:
      util.interpret_http_exception(e)

  @util.canonicalize_domain
  def get(self, type, source_short_name, string_id, *ids):
    source_cls = models.sources.get(source_short_name)
    if not source_cls:
      self.abort(400, "Source type '%s' not found. Known sources: %s" %
                 (source_short_name, filter(None, models.sources.keys())))

    self.source = source_cls.get_by_id(string_id)
    if not self.source:
      self.abort(400, 'Source %s %s not found' % (source_short_name, string_id))
    elif (self.source.status == 'disabled' or
          'listen' not in self.source.features):
      self.abort(400, 'Source %s is disabled for backfeed' % self.source.bridgy_path())

    format = self.request.get('format', 'html')
    if format not in ('html', 'json'):
      self.abort(400, 'Invalid format %s, expected html or json' % format)

    for id in ids:
      if not self.VALID_ID.match(id):
        self.abort(404, 'Invalid id %s' % id)

    try:
      obj = self.get_item(*ids)
    except models.DisableSource as e:
      self.abort(401, "Bridgy's access to your account has expired. Please visit https://brid.gy/ to refresh it!")
    except ValueError as e:
      self.abort(400, '%s error:\n%s' % (self.source.GR_CLASS.NAME, e))
    except Exception as e:
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
      self.abort(404, 'Not found: %s:%s %s %s' %
                      (source_short_name, string_id, type, ids))

    if self.source.is_blocked(obj):
      self.abort(410, 'That user is currently blocked')

    # use https for profile pictures so we don't cause SSL mixed mode errors
    # when serving over https.
    author = obj.get('author', {})
    image = author.get('image', {})
    url = image.get('url')
    if url:
      image['url'] = util.update_scheme(url, self)

    mf2_json = microformats2.object_to_json(obj, synthesize_content=False)

    # try to include the author's silo profile url
    author = first_props(mf2_json.get('properties', {})).get('author', {})
    author_uid = first_props(author.get('properties', {})).get('uid', '')
    if author_uid:
      parsed = util.parse_tag_uri(author_uid)
      if parsed:
        urls = author.get('properties', {}).setdefault('url', [])
        try:
          silo_url = self.source.gr_source.user_url(parsed[1])
          if silo_url not in microformats2.get_string_urls(urls):
            urls.append(silo_url)
        except NotImplementedError:  # from gr_source.user_url()
          pass

    # write the response!
    self.response.headers['Access-Control-Allow-Origin'] = '*'
    if format == 'html':
      self.response.headers['Content-Type'] = 'text/html; charset=utf-8'
      url = obj.get('url', '')
      self.response.out.write(TEMPLATE.substitute({
        'refresh': (('<meta http-equiv="refresh" content="0;url=%s">' % url)
                    if url else ''),
        'url': url,
        'body': microformats2.json_to_html(mf2_json),
        'title': self.get_title(obj),
      }))
    elif format == 'json':
      self.response.headers['Content-Type'] = 'application/json; charset=utf-8'
      self.response.out.write(json_dumps(mf2_json, indent=2))

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
    if obj:
      obj[property] = util.get_list(obj, property)
      existing = set(filter(None, (u.get('url') for u in obj[property])))
      obj[property] += [{'url': url, 'objectType': object_type} for url in urls
                        if url not in existing]


# Note that mention links are included in posts and comments, but not
# likes, reposts, or rsvps. Matches logic in poll() (step 4) in tasks.py!
class PostHandler(ItemHandler):
  cache = TTLCache(100, CACHE_TIME)
  @cachedmethod(lambda self: self.cache)
  def get_item(self, id):
    posts = self.source.get_activities(activity_id=id, user_id=self.source.key_id())
    if not posts:
      return None

    post = posts[0]
    originals, mentions = original_post_discovery.discover(
      self.source, post, fetch_hfeed=False)
    obj = post['object']
    obj['upstreamDuplicates'] = list(
      set(util.get_list(obj, 'upstreamDuplicates')) | originals)
    self.merge_urls(obj, 'tags', mentions, object_type='mention')
    return obj

class CommentHandler(ItemHandler):
  cache = TTLCache(100, CACHE_TIME)
  @cachedmethod(lambda self: self.cache)
  def get_item(self, post_id, id):
    post = self.get_post(post_id, fetch_replies=True)
    cmt = self.source.get_comment(
      id, activity_id=post_id, activity_author_id=self.source.key_id(),
      activity=post)
    if post:
      originals, mentions = original_post_discovery.discover(
        self.source, post, fetch_hfeed=False)
      self.merge_urls(cmt, 'inReplyTo', originals)
      self.merge_urls(cmt, 'tags', mentions, object_type='mention')
    return cmt


class LikeHandler(ItemHandler):
  cache = TTLCache(200, CACHE_TIME)
  @cachedmethod(lambda self: self.cache)
  def get_item(self, post_id, user_id):
    post = self.get_post(post_id, fetch_likes=True)
    like = self.source.get_like(self.source.key_id(), post_id, user_id,
                                activity=post)
    if post:
      originals, mentions = original_post_discovery.discover(
        self.source, post, fetch_hfeed=False)
      self.merge_urls(like, 'object', originals)
    return like

  def get_title(self, obj):
    """HOPEFULLY TEMPORARY hack: put liker name in <title>.

    ...as a workaround for https://github.com/snarfed/bridgy/issues/516 .
    """
    for o in obj, obj.get('object', {}):
      for field in 'actor', 'author':
        actor = o.get(field, {})
        name = actor.get('displayName') or actor.get('username')
        if name:
          return name

    return super(LikeHandler, self).get_title(obj)


class ReactionHandler(ItemHandler):
  cache = TTLCache(100, CACHE_TIME)
  @cachedmethod(lambda self: self.cache)
  def get_item(self, post_id, user_id, reaction_id):
    post = self.get_post(post_id)
    reaction = self.source.gr_source.get_reaction(
      self.source.key_id(), post_id, user_id, reaction_id, activity=post)
    if post:
      originals, mentions = original_post_discovery.discover(
        self.source, post, fetch_hfeed=False)
      self.merge_urls(reaction, 'object', originals)
    return reaction


class RepostHandler(ItemHandler):
  cache = TTLCache(100, CACHE_TIME)
  @cachedmethod(lambda self: self.cache)
  def get_item(self, post_id, share_id):
    post = self.get_post(post_id, fetch_shares=True)
    repost = self.source.gr_source.get_share(
      self.source.key_id(), post_id, share_id, activity=post)
    # webmention receivers don't want to see their own post in their
    # comments, so remove attachments before rendering.
    if repost and 'attachments' in repost:
      del repost['attachments']
    if post:
      originals, mentions = original_post_discovery.discover(
        self.source, post, fetch_hfeed=False)
      self.merge_urls(repost, 'object', originals)
    return repost


class RsvpHandler(ItemHandler):
  cache = TTLCache(100, CACHE_TIME)
  @cachedmethod(lambda self: self.cache)
  def get_item(self, event_id, user_id):
    event = self.source.gr_source.get_event(event_id)
    rsvp = self.source.gr_source.get_rsvp(
      self.source.key_id(), event_id, user_id, event=event)
    if event:
      originals, mentions = original_post_discovery.discover(
        self.source, event, fetch_hfeed=False)
      self.merge_urls(rsvp, 'inReplyTo', originals)
    return rsvp


ROUTES = [
  ('/(post)/(.+)/(.+)/(.+)', PostHandler),
  ('/(comment)/(.+)/(.+)/(.+)/(.+)', CommentHandler),
  ('/(like)/(.+)/(.+)/(.+)/(.+)', LikeHandler),
  ('/(react)/(.+)/(.+)/(.+)/(.+)/(.+)', ReactionHandler),
  ('/(repost)/(.+)/(.+)/(.+)/(.+)', RepostHandler),
  ('/(rsvp)/(.+)/(.+)/(.+)/(.+)', RsvpHandler),
]
