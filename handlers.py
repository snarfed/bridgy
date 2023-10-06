"""Common views, e.g. post and comment permalinks.

Docs: https://brid.gy/about#source-urls

URL paths are:

* ``/post/SITE/USER_ID/POST_ID``
  e.g. /post/flickr/212038/10100823411094363
* ``/comment/SITE/USER_ID/POST_ID/COMMENT_ID``
  e.g. /comment/twitter/snarfed_org/10100823411094363/999999
* ``/like/SITE/USER_ID/POST_ID/LIKED_BY_USER_ID``
  e.g. /like/twitter/snarfed_org/10100823411094363/999999
* ``/repost/SITE/USER_ID/POST_ID/REPOSTED_BY_USER_ID``
  e.g. /repost/twitter/snarfed_org/10100823411094363/999999
* ``/rsvp/SITE/USER_ID/EVENT_ID/RSVP_USER_ID``
  e.g. /rsvp/facebook/212038/12345/67890
"""
import datetime
import logging
import re
import string

from flask import request
from flask.views import View
from granary import microformats2
from granary.microformats2 import first_props
from oauth_dropins.webutil import flask_util
from oauth_dropins.webutil.flask_util import error
from oauth_dropins.webutil.util import json_dumps, json_loads

from flask_app import app, cache
import models
import original_post_discovery
import util

logger = logging.getLogger(__name__)

CACHE_TIME = datetime.timedelta(minutes=15)

TEMPLATE = string.Template("""\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
$refresh
<title>$title</title>
<style type="text/css">
body {
  display: none;
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


class Item(View):
  """Fetches a post, repost, like, or comment and serves it as mf2 HTML or JSON.
  """
  source = None

  VALID_ID = re.compile(r'^[\w.+:@=<>-]+$')

  def get_item(self, **kwargs):
    """Fetches and returns an object from the given source.

    To be implemented by subclasses.

    Args:
      source: :class:`models.Source` subclass
      id: str

    Returns:
      ActivityStreams object dict
    """
    raise NotImplementedError()

  def get_post(self, id, **kwargs):
    """Fetch a post.

    Args:
      id: str, site-specific post id
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
      logger.warning(f'Source post {id} not found')
    except AssertionError:
      raise
    except Exception as e:
      util.interpret_http_exception(e)

  @flask_util.cached(cache, CACHE_TIME)
  def dispatch_request(self, site, key_id, **kwargs):
    """Handle HTTP request."""
    source_cls = models.sources.get(site)
    if not source_cls:
      error(f"Source type '{site}' not found. Known sources: {[s for s in models.sources.keys() if s]}")

    self.source = source_cls.get_by_id(key_id)
    if not self.source:
      error(f'Source {site} {key_id} not found')
    elif (self.source.status == 'disabled' or
          'listen' not in self.source.features) and self.source.SHORT_NAME != 'twitter':
      error(f'Source {self.source.bridgy_path()} is disabled for backfeed')

    format = request.values.get('format', 'html')
    if format not in ('html', 'json'):
      error(f'Invalid format {format}, expected html or json')

    for id in kwargs.values():
      if not self.VALID_ID.match(id):
        error(f'Invalid id {id}', 404)

    # short circuit downstream fetches for HEADs.
    #
    # this was originally implemented as a separate handler, but Flask overrides
    # that when it automatically adds HEAD to GET routes, so this is their
    # recommended approach.
    # https://github.com/pallets/flask/issues/4395#issuecomment-1032882475
    if request.method == 'HEAD':
      return ''

    try:
      obj = self.get_item(**kwargs)
    except models.DisableSource:
      error("Bridgy's access to your account has expired. Please visit https://brid.gy/ to refresh it!", 401)
    except ValueError as e:
      error(f'{self.source.GR_CLASS.NAME} error: {e}')

    if not obj:
      error(f'Not found: {site}:{key_id} {kwargs}', 404)

    if self.source.is_blocked(obj):
      error('That user is currently blocked', 410)

    # use https for profile pictures so we don't cause SSL mixed mode errors
    # when serving over https.
    author = obj.get('author', {})
    image = author.get('image', {})
    url = image.get('url')
    if url:
      image['url'] = util.update_scheme(url, request)

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
    if format == 'html':
      url = obj.get('url', '')
      return TEMPLATE.substitute({
        'refresh': (f'<meta http-equiv="refresh" content="0;url={url}">'
                    if url else ''),
        'url': url,
        'body': microformats2.json_to_html(mf2_json),
        'title': obj.get('title') or obj.get('content') or 'Bridgy Response',
      })
    elif format == 'json':
      return mf2_json

  def merge_urls(self, obj, property, urls, object_type='article'):
    """Updates an object's ActivityStreams URL objects in place.

    Adds all URLs in urls that don't already exist in ``obj[property]``\.

    ActivityStreams schema details:
    http://activitystrea.ms/specs/json/1.0/#id-comparison

    Args:
      obj (dict): ActivityStreams object to merge URLs into
      property (str): property to merge URLs into
      urls (sequence of str): URLs to add
      object_type (str): stored as the objectType alongside each URL
    """
    if obj:
      obj[property] = util.get_list(obj, property)
      existing = set(filter(None, (u.get('url') for u in obj[property])))
      obj[property] += [{'url': url, 'objectType': object_type} for url in urls
                        if url not in existing]


# Note that mention links are included in posts and comments, but not
# likes, reposts, or rsvps. Matches logic in poll() (step 4) in tasks.py!
class Post(Item):
  def get_item(self, post_id):
    posts = None

    if self.source.SHORT_NAME == 'twitter':
      resp = models.Response.get_by_id(self.source.gr_source.tag_uri(post_id))
      if resp and resp.response_json:
        posts = [json_loads(resp.response_json)]
    else:
      posts = self.source.get_activities(activity_id=post_id,
                                         user_id=self.source.key_id())
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


class Comment(Item):
  def get_item(self, post_id, comment_id):
    if self.source.SHORT_NAME == 'twitter':
      cmt = post = None
      resp = models.Response.get_by_id(self.source.gr_source.tag_uri(comment_id))
      if resp and resp.response_json:
        cmt = json_loads(resp.response_json)
        if resp.activities_json:
          for activity in resp.activities_json:
            activity = json_loads(activity)
            if activity.get('id') == self.source.gr_source.tag_uri(post_id):
              post = activity

    else:
      fetch_replies = not self.source.gr_source.OPTIMIZED_COMMENTS
      post = self.get_post(post_id, fetch_replies=fetch_replies)
      has_replies = (post.get('object', {}).get('replies', {}).get('items')
                     if post else False)
      cmt = self.source.get_comment(
        comment_id, activity_id=post_id, activity_author_id=self.source.key_id(),
        activity=post if fetch_replies or has_replies else None)

    if post:
      originals, mentions = original_post_discovery.discover(
        self.source, post, fetch_hfeed=False)
      self.merge_urls(cmt, 'inReplyTo', originals)
      self.merge_urls(cmt, 'tags', mentions, object_type='mention')
    return cmt


class Like(Item):
  def get_item(self, post_id, user_id):
    post = self.get_post(post_id, fetch_likes=True)
    like = self.source.get_like(self.source.key_id(), post_id, user_id,
                                activity=post)
    if post:
      originals, mentions = original_post_discovery.discover(
        self.source, post, fetch_hfeed=False)
      self.merge_urls(like, 'object', originals)
    return like


class Reaction(Item):
  def get_item(self, post_id, user_id, reaction_id):
    post = self.get_post(post_id)
    reaction = self.source.gr_source.get_reaction(
      self.source.key_id(), post_id, user_id, reaction_id, activity=post)
    if post:
      originals, mentions = original_post_discovery.discover(
        self.source, post, fetch_hfeed=False)
      self.merge_urls(reaction, 'object', originals)
    return reaction


class Repost(Item):
  def get_item(self, post_id, share_id):
    if self.source.SHORT_NAME == 'twitter':
      repost = post = None
      resp = models.Response.get_by_id(self.source.gr_source.tag_uri(share_id))
      if resp and resp.response_json:
        repost = json_loads(resp.response_json)
        if resp.activities_json:
          for activity in resp.activities_json:
            activity = json_loads(activity)
            if activity.get('id') == self.source.gr_source.tag_uri(post_id):
              post = activity

    else:
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


class Rsvp(Item):
  def get_item(self, event_id, user_id):
    event = self.source.gr_source.get_event(event_id)
    rsvp = self.source.gr_source.get_rsvp(
      self.source.key_id(), event_id, user_id, event=event)
    if event:
      originals, mentions = original_post_discovery.discover(
        self.source, event, fetch_hfeed=False)
      self.merge_urls(rsvp, 'inReplyTo', originals)
    return rsvp


app.add_url_rule('/post/<site>/<key_id>/<post_id>',
                 view_func=Post.as_view('post'))
app.add_url_rule('/comment/<site>/<key_id>/<post_id>/<comment_id>',
                 view_func=Comment.as_view('comment'))
app.add_url_rule('/like/<site>/<key_id>/<post_id>/<user_id>',
                 view_func=Like.as_view('like'))
app.add_url_rule('/react/<site>/<key_id>/<post_id>/<user_id>/<reaction_id>',
                 view_func=Reaction.as_view('react'))
app.add_url_rule('/repost/<site>/<key_id>/<post_id>/<share_id>',
                 view_func=Repost.as_view('repost'))
app.add_url_rule('/rsvp/<site>/<key_id>/<event_id>/<user_id>',
                 view_func=Rsvp.as_view('rsvp'))


