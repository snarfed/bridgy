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
                 (source_short_name, models.sources))

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

  def add_original_post_urls(self, post, obj, prop):
    """Extracts original post URLs and adds them to an object, in place.

    If the post object has upstreamDuplicates, *only* they are considered
    original post URLs and added as tags with objectType 'article', and the
    post's own links and 'article' tags are added with objectType 'mention'.

    Args:
      post: ActivityStreams post object to get original post URLs from
      obj: ActivityStreams post object to add original post URLs to
      prop: string property name in obj to add the original post URLs to
    """
    original_post_discovery.discover(self.source, post, fetch_hfeed=False)
    tags = [tag for tag in post['object'].get('tags', [])
            if 'url' in tag and tag['objectType'] == 'article']
    upstreams = post['object'].get('upstreamDuplicates', [])

    if not isinstance(obj.setdefault(prop, []), list):
      obj[prop] = [obj[prop]]
    if upstreams:
      obj[prop] += [{'url': url, 'objectType': 'article'} for url in upstreams]
      obj.setdefault('tags', []).extend(
        [{'url': tag.get('url'), 'objectType': 'mention'} for tag in tags])
    else:
      obj[prop] += tags

    # check for redirects, and if there are any follow them and add final urls
    # in addition to the initial urls.
    seen = set()
    tags = obj.get('tags', [])
    for url_list in obj[prop], tags:
      for url_obj in url_list:
        url = util.clean_webmention_url(url_obj.get('url', ''))
        if not url or url in seen:
          continue
        seen.add(url)
        # when debugging locally, replace my (snarfed.org) URLs with localhost
        url_obj['url'] = url = util.replace_test_domains_with_localhost(url)
        resolved, _, send = util.get_webmention_target(url)
        if send and resolved != url and resolved not in seen:
          seen.add(resolved)
          url_list.append({'url': resolved, 'objectType': url_obj.get('objectType')})

    # if the http version of a link is in upstreams but the https one is just a
    # mention, or vice versa, promote them both to upstream.
    # https://github.com/snarfed/bridgy/issues/290
    #
    # TODO: for links that came from resolving redirects above, this doesn't
    # also catch the initial pre-redirect link. ah well.
    prop_schemeful = set(tag['url'] for tag in obj[prop] if tag.get('url'))
    prop_schemeless = set(util.schemeless(url) for url in prop_schemeful)

    for url_obj in copy.copy(tags):
      url = url_obj.get('url', '')
      schemeless = util.schemeless(url)
      if schemeless in prop_schemeless and url not in prop_schemeful:
        obj[prop].append(url_obj)
        tags.remove(url_obj)
        prop_schemeful.add(url)

    logging.info('After original post discovery, urls are: %s', seen)


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
    post = self.get_post(post_id)
    if post:
      self.add_original_post_urls(post, cmt, 'inReplyTo')
    return cmt


class LikeHandler(ItemHandler):
  def get_item(self, post_id, user_id):
    like = self.source.get_like(self.source.key.string_id(), post_id, user_id)
    if not like:
      return None
    post = self.get_post(post_id)
    if post:
      self.add_original_post_urls(post, like, 'object')
    return like


class RepostHandler(ItemHandler):
  def get_item(self, post_id, share_id):
    repost = self.source.get_share(self.source.key.string_id(), post_id, share_id)
    if not repost:
      return None
    post = self.get_post(post_id)
    if post:
      self.add_original_post_urls(post, repost, 'object')
    return repost


class RsvpHandler(ItemHandler):
  def get_item(self, event_id, user_id):
    rsvp = self.source.get_rsvp(self.source.key.string_id(), event_id, user_id)
    if not rsvp:
      return None
    event = self.get_post(event_id, source_fn=self.source.get_event)
    if event:
      self.add_original_post_urls(event, rsvp, 'inReplyTo')
    return rsvp


application = webapp2.WSGIApplication([
    ('/(post)/(.+)/(.+)/(.+)', PostHandler),
    ('/(comment)/(.+)/(.+)/(.+)/(.+)', CommentHandler),
    ('/(like)/(.+)/(.+)/(.+)/(.+)', LikeHandler),
    ('/(repost)/(.+)/(.+)/(.+)/(.+)', RepostHandler),
    ('/(rsvp)/(.+)/(.+)/(.+)/(.+)', RsvpHandler),
    ], debug=appengine_config.DEBUG)
