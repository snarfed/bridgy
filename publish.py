"""Publishes webmentions into the silos.

Webmention spec: http://webmention.org/

Bridgy request and response details: https://brid.gy/about#response
"""
import collections
import logging
import pprint
import re
import urllib.request, urllib.parse, urllib.error

from flask import render_template, request
from google.cloud import ndb
from granary import microformats2
from granary import source as gr_source
import grpc
from oauth_dropins import (
  flickr as oauth_flickr,
  github as oauth_github,
  mastodon as oauth_mastodon,
  twitter as oauth_twitter,
)
from oauth_dropins.webutil import appengine_info
from oauth_dropins.webutil import flask_util
from oauth_dropins.webutil.flask_util import flash
from oauth_dropins.webutil.util import json_dumps, json_loads
from werkzeug.exceptions import HTTPException

from flask_app import app
from flickr import Flickr
from github import GitHub
from instagram import Instagram
from mastodon import Mastodon
from models import Publish, PublishedPage
from twitter import Twitter
import models
import util
from util import redirect
import webmention

logger = logging.getLogger(__name__)


SOURCES = (Flickr, GitHub, Mastodon, Twitter)
SOURCE_NAMES = {cls.SHORT_NAME: cls for cls in SOURCES}
SOURCE_DOMAINS = {cls.GR_CLASS.DOMAIN: cls for cls in SOURCES}
# image URLs matching this regexp should be ignored.
# (This matches Wordpress Jetpack lazy loaded image placeholders.)
# https://github.com/snarfed/bridgy/issues/798
IGNORE_IMAGE_RE = re.compile(r'.*/lazy-images/images/1x1\.trans\.gif$')

PUBLISHABLE_TYPES = frozenset((
  'h-checkin',
  'h-entry',
  'h-event',
  'h-geo',
  'h-item',
  'h-listing',
  'h-product',
  'h-recipe',
  'h-resume',
  'h-review',
))


class CollisionError(RuntimeError):
  """Multiple publish requests for the same page at the same time."""
  pass


class PublishBase(webmention.Webmention):
  """Base handler for both previews and publishes.

  Subclasses must set the :attr:`PREVIEW` attribute to True or False. They may
  also override other methods.

  Attributes:
    fetched: :class:`requests.Response` from fetching source_url
    shortlink: rel-shortlink found in the original post, if any
  """
  PREVIEW = None

  shortlink = None
  source = None

  def authorize(self):
    """Returns True if the current user is authorized for this request.

    Otherwise, should call :meth:`self.error()` to provide an appropriate
    error message.
    """
    return True

  def source_url(self):
    return request.values['source'].strip()

  def target_url(self):
    return request.values['target'].strip()

  def include_link(self, item):
    val = request.values.get('bridgy_omit_link', None)

    if val is None:
      # _run has already parsed and validated the target URL
      vals = urllib.parse.parse_qs(urllib.parse.urlparse(self.target_url()).query)\
                     .get('bridgy_omit_link')
      val = vals[0] if vals else None

    if val is None:
      vals = item.get('properties', {}).get('bridgy-omit-link')
      val = vals[0] if vals else None

    return (gr_source.INCLUDE_LINK if val is None or val.lower() == 'false'
            else gr_source.INCLUDE_IF_TRUNCATED if val.lower() == 'maybe'
            else gr_source.OMIT_LINK)

  def ignore_formatting(self, item):
    val = request.values.get('bridgy_ignore_formatting', None)

    if val is None:
      # _run has already parsed and validated the target URL
      vals = urllib.parse.parse_qs(urllib.parse.urlparse(self.target_url()).query)\
                     .get('bridgy_ignore_formatting')
      val = vals[0] if vals else None

    if val is not None:
      return val.lower() in ('', 'true')

    return 'bridgy-ignore-formatting' in item.get('properties', {})

  def maybe_inject_silo_content(self, item):
    props = item.setdefault('properties', {})
    silo_content = props.get(f'bridgy-{self.source.SHORT_NAME}-content', [])
    if silo_content:
      props['content'] = silo_content
      props.pop('name', None)
      props.pop('summary', None)

  def _run(self):
    """Returns CreationResult on success, None otherwise."""
    logging.info(f'Params: {list(request.values.items())}')
    assert self.PREVIEW in (True, False)

    # parse and validate target URL
    try:
      parsed = urllib.parse.urlparse(self.target_url())
    except BaseException:
      self.error(f'Could not parse target URL {self.target_url()}')

    domain = parsed.netloc
    path_parts = parsed.path.rsplit('/', 1)
    source_cls = SOURCE_NAMES.get(path_parts[-1])
    if (domain not in util.DOMAINS or
        len(path_parts) != 2 or path_parts[0] != '/publish' or not source_cls):
      self.error(
        'Target must be brid.gy/publish/[flickr,github,mastodon,twitter]')
    elif source_cls == Instagram:
      self.error(f'Sorry, {source_cls.GR_CLASS.NAME} is not supported.')

    # resolve source URL
    source_url = self.source_url()
    resolved_url, domain, ok = util.get_webmention_target(
      source_url, replace_test_domains=False)
    # show nice error message if they're trying to publish a silo post
    if domain in SOURCE_DOMAINS:
      return self.error(
        f"Looks like that's a {SOURCE_DOMAINS[domain].GR_CLASS.NAME} URL. Try one from your web site instead!")
    elif not ok:
      return self.error(f'Unsupported source URL {resolved_url}')
    elif not domain:
      return self.error(f'Could not parse source URL {resolved_url}')

    # look up source by domain
    self.source = self._find_source(source_cls, resolved_url, domain)

    content_param = f'bridgy_{self.source.SHORT_NAME}_content'
    if content_param in request.values:
      return self.error(f'The {content_param} parameter is not supported')

    # show nice error message if they're trying to publish their home page
    for domain_url in self.source.domain_urls:
      domain_url_parts = urllib.parse.urlparse(domain_url)
      for check_url in resolved_url, source_url:
        parts = urllib.parse.urlparse(check_url)
        if (parts.netloc == domain_url_parts.netloc and
            parts.path.strip('/') == domain_url_parts.path.strip('/') and
            not parts.query):
          return self.error(
            "Looks like that's your home page. Try one of your posts instead!")

    # done with the sanity checks, create the Publish entity
    self.entity = self.get_or_add_publish_entity(resolved_url)
    if not self.entity:
      return None

    # fetch the source page!
    fragment = urllib.parse.urlparse(source_url).fragment
    try:
      resp = self.fetch_mf2(resolved_url, id=fragment, raise_errors=True)
    except HTTPException:
      # raised by us, probably via self.error()
      raise
    except BaseException as e:
      status, body = util.interpret_http_exception(e)
      if status == '410':
        return self.delete(resolved_url)
      return self.error(f'Could not fetch source URL {resolved_url}')

    if not resp:
      return
    self.fetched, mf2 = resp

    # check that we haven't already published this URL. (we can't do this before
    # fetching because it might be a 410 delete, which we only know by fetching.)
    if (self.entity.status == 'complete' and self.entity.type != 'preview' and
        not self.PREVIEW and not appengine_info.LOCAL):
      return self.error("Sorry, you've already published that page, and Bridgy Publish doesn't support updating existing posts. Details: https://github.com/snarfed/bridgy/issues/84",
                        extra_json={'original': self.entity.published})

    # find rel-shortlink, if any
    # http://microformats.org/wiki/rel-shortlink
    # https://github.com/snarfed/bridgy/issues/173
    shortlinks = mf2['rels'].get('shortlink')
    if shortlinks:
      self.shortlink = urllib.parse.urljoin(resolved_url, shortlinks[0])

    # loop through each item and its children and try to preview/create it. if
    # it fails, try the next one. break after the first one that works.
    result = None
    types = set()
    queue = collections.deque(mf2.get('items', []))
    while queue:
      item = queue.popleft()
      item_types = set(item.get('type'))
      if 'h-feed' in item_types and 'h-entry' not in item_types:
        queue.extend(item.get('children', []))
        continue
      elif not item_types & PUBLISHABLE_TYPES:
        types = types.union(item_types)
        continue

      try:
        result = self.attempt_single_item(item)
        if self.entity.published:
          break
        if result.abort:
          if result.error_plain:
            self.error(result.error_plain, html=result.error_html, data=item)
          return
        # try the next item
        for embedded in ('rsvp', 'invitee', 'repost', 'repost-of', 'like',
                         'like-of', 'in-reply-to'):
          if embedded in item.get('properties', []):
            item_types.add(embedded)
        logging.info(
          'Object type(s) %s not supported; error=%s; trying next.',
          item_types, result.error_plain)
        types = types.union(item_types)
        queue.extend(item.get('children', []))
      except HTTPException:
        # raised by us, probably via self.error()
        raise
      except BaseException as e:
        code, body = util.interpret_http_exception(e)
        if code in self.source.DISABLE_HTTP_CODES or isinstance(e, models.DisableSource):
          # the user deauthorized the bridgy app, or the token expired, so
          # disable this source.
          logging.warning(f'Disabling source due to: {e}', exc_info=True)
          self.source.status = 'disabled'
          self.source.put()
        if isinstance(e, (NotImplementedError, ValueError, urllib.error.URLError)):
          code = '400'
        elif not code:
          raise
        msg = f"Error: {body or ''} {e}"
        return self.error(msg, status=code, report=code not in
                          ('400', '403', '404', '406', '502', '503', '504'))

    if not self.entity.published:  # tried all the items
      types.discard('h-entry')
      types.discard('h-note')
      if types:
        msg = f"{source_cls.GR_CLASS.NAME} doesn't support type(s) {' + '.join(types)}, or no content was found."
        return self.error(msg, data=mf2)
      else:
        msg = 'Could not find content in <a href="http://microformats.org/wiki/h-entry">h-entry</a> or any other element!'
        return self.error(msg, html=msg, data=mf2)

    # write results to datastore, but don't overwrite a previous publish with a
    # preview.
    if not (self.PREVIEW and self.entity.type != 'preview'):
      self.entity.status = 'complete'
      self.entity.put()

    return result

  def _find_source(self, source_cls, url, domain):
    """Returns the source that should publish a post URL, or None if not found.

    Args:
      source_cls: :class:`models.Source` subclass for this silo
      url: string
      domain: string, url's domain

    Returns: :class:`models.Source`
    """
    domain = domain.lower()
    if util.domain_or_parent_in(domain, util.DOMAINS):
      return self.error(f'Source URL should be on your own site, not {domain}')

    sources = source_cls.query().filter(source_cls.domains == domain).fetch(100)
    if not sources:
      msg = f'Could not find <b>{source_cls.GR_CLASS.NAME}</b> account for <b>{domain}</b>. Check that your {source_cls.GR_CLASS.NAME} profile has {domain} in its <em>web site</em> or <em>link</em> field, then try signing up again.'
      return self.error(msg, html=msg)

    current_url = ''
    sources_ready = []
    best_match = None
    for source in sources:
      logging.info(f'Source: {source.bridgy_url()} , features {source.features}, status {source.status}, poll status {source.poll_status}')
      if source.status != 'disabled' and 'publish' in source.features:
        # use a source that has a domain_url matching the url provided,
        # including path. find the source with the closest match.
        sources_ready.append(source)
        schemeless_url = util.schemeless(url.lower()).strip('/')
        for domain_url in source.domain_urls:
          schemeless_domain_url = util.schemeless(domain_url.lower()).strip('/')
          if (schemeless_url.startswith(schemeless_domain_url) and
              len(domain_url) > len(current_url)):
            current_url = domain_url
            best_match = source

    if best_match:
      return best_match

    if sources_ready:
      msg = f'No account found that matches {util.pretty_link(url)}. Check that <a href="{util.host_url("/about#profile-link")}">the web site URL is in your silo profile</a>, then <a href="{request.host_url}">sign up again</a>.'
    else:
      msg = f'Publish is not enabled for your account. <a href="{request.host_url}">Try signing up!</a>'
    self.error(msg, html=msg)

  def attempt_single_item(self, item):
    """Attempts to preview or publish a single mf2 item.

    Args:
      item: mf2 item dict from mf2py

    Returns:
      CreationResult
    """
    self.maybe_inject_silo_content(item)
    obj = microformats2.json_to_object(item)

    ignore_formatting = self.ignore_formatting(item)
    if ignore_formatting:
      prop = microformats2.first_props(item.get('properties', {}))
      content = microformats2.get_text(prop.get('content'))
      if content:
        obj['content'] = content.strip()

    # which original post URL to include? in order of preference:
    # 1. rel-shortlink (background: https://github.com/snarfed/bridgy/issues/173)
    # 2. original user-provided URL if it redirected
    # 3. u-url if available
    # 4. actual final fetched URL
    if self.shortlink:
      obj['url'] = self.shortlink
    elif self.source_url() != self.fetched.url:
      obj['url'] = self.source_url()
    elif 'url' not in obj:
      obj['url'] = self.fetched.url
    logging.debug(f'Converted to ActivityStreams object: {json_dumps(obj, indent=2)}')

    # posts and comments need content
    obj_type = obj.get('objectType')
    if (obj_type in ('note', 'article', 'comment') and
        (not obj.get('content') and not obj.get('summary')
         and not obj.get('displayName'))):
      return gr_source.creation_result(
        abort=False,
        error_plain=f'Could not find content in {self.fetched.url}',
        error_html=f'Could not find <a href="http://microformats.org/">content</a> in {self.fetched.url}')

    self.preprocess(obj)

    include_link = self.include_link(item)

    if not self.authorize():
      return gr_source.creation_result(abort=True)

    if self.PREVIEW:
      result = self.source.gr_source.preview_create(
        obj, include_link=include_link, ignore_formatting=ignore_formatting)
      previewed = result.content or result.description
      if self.entity.type == 'preview':
        self.entity.published = previewed
      if not previewed:
        return result  # there was an error
      return self._render_preview(result, include_link=include_link)

    else:
      result = self.source.gr_source.create(
        obj, include_link=include_link, ignore_formatting=ignore_formatting)
      self.entity.published = result.content
      if not result.content:
        return result  # there was an error
      if 'url' not in self.entity.published:
        self.entity.published['url'] = obj.get('url')
      self.entity.type = self.entity.published.get('type') or models.get_type(obj)

      ret = json_dumps(self.entity.published, indent=2)
      logging.info(f'Returning {ret}')
      return gr_source.creation_result(ret)

  def delete(self, source_url):
    """Attempts to delete or preview delete a published post.

    Args:
      source_url: string, original post URL

    Returns:
      dict response data with at least id and url
    """
    assert self.entity
    if ((self.entity.status != 'complete' or self.entity.type == 'preview') and
        not appengine_info.LOCAL):
      return self.error(f"Can't delete this post from {self.source.gr_source.NAME} because Bridgy Publish didn't originally POSSE it there")

    id = self.entity.published.get('id')
    url = self.entity.published.get('url')
    if not id and url:
      id = self.source.gr_source.post_id(url)

    if not id:
      return self.error(
        f"Bridgy Publish can't find the id of the {self.source.gr_source.NAME} post that it originally published for {source_url}")

    if self.PREVIEW:
      try:
        return self._render_preview(self.source.gr_source.preview_delete(id))
      except NotImplementedError:
        return self.error(f"Sorry, deleting isn't supported for {self.source.gr_source.NAME} yet")

    logging.info(f'Deleting silo post id {id}')
    self.entity = models.Publish(parent=self.entity.key.parent(),
                                 source=self.source.key, type='delete')
    self.entity.put()
    logging.debug(f"Publish entity for delete: {self.entity.key.urlsafe().decode()}")

    resp = self.source.gr_source.delete(id)
    resp.content.setdefault('id', id)
    resp.content.setdefault('url', url)
    logging.info(resp.content)
    self.entity.published = resp.content
    self.entity.status = 'deleted'
    self.entity.put()
    return resp

  def preprocess(self, activity):
    """Preprocesses an item before trying to publish it.

    Specifically, expands inReplyTo/object URLs with rel=syndication URLs.

    Args:
      activity: an ActivityStreams activity or object being published
    """
    self.source.preprocess_for_publish(activity)
    self.expand_target_urls(activity)

    activity['image'] = [img for img in util.get_list(activity, 'image')
                         if not IGNORE_IMAGE_RE.match(img.get('url', ''))]
    if not activity['image']:
      del activity['image']

  def expand_target_urls(self, activity):
    """Expand the inReplyTo or object fields of an ActivityStreams object
    by fetching the original and looking for rel=syndication URLs.

    This method modifies the dict in place.

    Args:
      activity: an ActivityStreams dict of the activity being published
    """
    for field in ('inReplyTo', 'object'):
      # microformats2.json_to_object de-dupes, no need to do it here
      objs = activity.get(field)
      if not objs:
        continue

      if isinstance(objs, dict):
        objs = [objs]

      augmented = list(objs)
      for obj in objs:
        url = obj.get('url')
        if not url:
          continue

        parsed = urllib.parse.urlparse(url)
        # ignore home pages. https://github.com/snarfed/bridgy/issues/760
        if parsed.path in ('', '/'):
          continue

        # get_webmention_target weeds out silos and non-HTML targets
        # that we wouldn't want to download and parse
        url, _, ok = util.get_webmention_target(url)
        if not ok:
          continue

        logging.debug(f'expand_target_urls fetching field={field}, url={url}')
        try:
          mf2 = util.fetch_mf2(url)
        except AssertionError:
          raise  # for unit tests
        except HTTPException:
          # raised by us, probably via self.error()
          raise
        except BaseException:
          # it's not a big deal if we can't fetch an in-reply-to url
          logging.info(f'expand_target_urls could not fetch field={field}, url={url}', exc_info=True)
          continue

        synd_urls = mf2['rels'].get('syndication', [])

        # look for syndication urls in the first h-entry
        queue = collections.deque(mf2.get('items', []))
        while queue:
          item = queue.popleft()
          item_types = set(item.get('type', []))
          if 'h-feed' in item_types and 'h-entry' not in item_types:
            queue.extend(item.get('children', []))
            continue

          # these can be urls or h-cites
          synd_urls += microformats2.get_string_urls(
            item.get('properties', {}).get('syndication', []))

        logging.debug(f'expand_target_urls found rel=syndication for url={url} : {synd_urls!r}')
        augmented += [{'url': u} for u in synd_urls]

      activity[field] = augmented

  def get_or_add_publish_entity(self, source_url):
    """Creates and stores :class:`models.Publish` entity.

    ...and if necessary, :class:`models.PublishedPage` entity.

    Args:
      source_url: string
    """
    try:
      return self._get_or_add_publish_entity(source_url)
    except CollisionError:
      return self.error("You're already publishing that post in another request.",
                        status=429)
    except Exception as e:
      code = getattr(e, 'code', None)
      details = getattr(e, 'details', None)
      logging.info((code and code(), details and details()))
      if (code and code() == grpc.StatusCode.ABORTED and
          details and 'too much contention' in details()):
        return self.error("You're already publishing that post in another request.",
                          status=429)
      raise

  @ndb.transactional()
  def _get_or_add_publish_entity(self, source_url):
    page = PublishedPage.get_or_insert(source_url)

    # Detect concurrent publish request for the same page
    # https://github.com/snarfed/bridgy/issues/996
    pending = Publish.query(
        Publish.status == 'new', Publish.type != 'preview',
        Publish.source == self.source.key, ancestor=page.key).get()
    if pending:
      logging.warning(f'Collided with publish: {pending.key.urlsafe().decode()}')
      raise CollisionError()

    entity = Publish.query(
      Publish.status == 'complete', Publish.type != 'preview',
      Publish.source == self.source.key, ancestor=page.key).get()
    if entity is None:
      entity = Publish(parent=page.key, source=self.source.key)
      if self.PREVIEW:
        entity.type = 'preview'
      entity.put()

    logging.debug(f"Publish entity: {entity.key.urlsafe().decode()}")
    return entity

  def _render_preview(self, result, include_link=False):
    """Renders a preview CreationResult as HTML.

    Args:
      result: CreationResult
      include_link: boolean

    Returns: CreationResult with the rendered HTML in content
    """
    state = {
      'source_key': self.source.key.urlsafe().decode(),
      'source_url': self.source_url(),
      'target_url': self.target_url(),
      'include_link': include_link,
    }
    vars = {
      'source': util.preprocess_source(self.source),
      'preview': result.content,
      'description': result.description,
      'webmention_endpoint': util.host_url('/publish/webmention'),
      'state': util.encode_oauth_state(state),
      **state,
    }
    logging.info(f'Rendering preview with template vars {pprint.pformat(vars)}')
    return gr_source.creation_result(render_template('preview.html', **vars))


class Preview(PublishBase):
  """Renders a preview HTML snippet of how a webmention would be handled.
  """
  PREVIEW = True

  def dispatch_request(self):
    try:
      result = self._run()
      return result.content if result and result.content else r'¯\_(ツ)_/¯'
    except HTTPException as e:
      return e.description, e.code

  def authorize(self):
    from_source = util.load_source()
    if from_source.key != self.source.key:
      msg = f'Try publishing that page from <a href="{self.source.bridgy_path()}">{self.source.label()}</a> instead.'
      self.error(msg, html=msg)
      return False

    return True

  def include_link(self, item):
    # always use query param because there's a checkbox in the UI
    val = request.values.get('bridgy_omit_link', None)
    return (gr_source.INCLUDE_LINK if val is None or val.lower() == 'false'
            else gr_source.INCLUDE_IF_TRUNCATED if val.lower() == 'maybe'
            else gr_source.OMIT_LINK)

  def error(self, error, html=None, status=400, data=None, report=False, **kwargs):
    error = html or util.linkify(error)
    logging.info(f'publish: {error}')
    if report:
      self.report_error(error, status=status)
    flask_util.error(error, status=status)


class Send(PublishBase):
  """Interactive publish handler. Redirected to after each silo's OAuth dance.

  Note that this is GET, not POST, since HTTP redirects always GET.
  """
  PREVIEW = False

  def finish(self, auth_entity, state=None):
    self.state = util.decode_oauth_state(state)
    if not state:
      self.error('If you want to publish or preview, please approve the prompt.')
      return redirect('/')

    source = ndb.Key(urlsafe=self.state['source_key']).get()
    if auth_entity is None:
      self.error('If you want to publish or preview, please approve the prompt.')
    elif not auth_entity.is_authority_for(source.auth_entity):
      self.error(f'Please log into {source.GR_CLASS.NAME} as {source.name} to publish that page.')
    else:
      result = self._run()
      if result and result.content:
        flash(f"Done! <a href=\"{self.entity.published.get('url')}\">Click here to view.</a>")
        granary_message = self.entity.published.get('granary_message')
        if granary_message:
          flash(granary_message)
      # otherwise error() added an error message

    return redirect(source.bridgy_url())

  def source_url(self):
    return self.state['source_url']

  def target_url(self):
    return self.state['target_url']

  def include_link(self, item):
    return self.state['include_link']

  def error(self, error, html=None, status=400, data=None, report=False, **kwargs):
    logging.info(f'publish: {error}')
    error = html or util.linkify(error)
    flash(f'{error}')
    if report:
      self.report_error(error, status=status)


# We want Callback.get() and Send.finish(), so put
# Callback first and override finish.
class FlickrSend(oauth_flickr.Callback, Send):
  finish = Send.finish


class GitHubSend(oauth_github.Callback, Send):
  finish = Send.finish


class MastodonSend(oauth_mastodon.Callback, Send):
  finish = Send.finish


class TwitterSend(oauth_twitter.Callback, Send):
  finish = Send.finish


class Webmention(PublishBase):
  """Accepts webmentions and translates them to publish requests."""
  PREVIEW = False

  def dispatch_request(self):
    result = self._run()
    if result:
      return result.content, 201, {
        'Content-Type': 'application/json',
        'Location': self.entity.published['url'],
      }

    return ''

  def authorize(self):
    """Check for a backlink to brid.gy/publish/SILO."""
    bases = set()
    if request.host == 'brid.gy':
      bases.add('brid.gy')
      bases.add('www.brid.gy')  # also accept www
    else:
      bases.add(request.host)

    expected = [f'{base}/publish/{self.source.SHORT_NAME}' for base in bases]

    if self.entity.html:
      for url in expected:
        if url in self.entity.html or urllib.parse.quote(url, safe='') in self.entity.html:
          return True

    self.error(f"Couldn't find link to {expected[0]}")
    return False

  def error(self, error, **kwargs):
    logging.info(f'publish: {error}')
    return super().error(error, **kwargs)


app.add_url_rule('/publish/preview', view_func=Preview.as_view('publish_preview'), methods=['POST'])
app.add_url_rule('/publish/webmention', view_func=Webmention.as_view('publish_webmention'), methods=['POST'])
app.add_url_rule('/publish/flickr/finish', view_func=FlickrSend.as_view('publish_flickr_finish', 'unused'))
app.add_url_rule('/publish/github/finish', view_func=GitHubSend.as_view('publish_github_finish', 'unused'))
app.add_url_rule('/publish/mastodon/finish', view_func=MastodonSend.as_view('publish_mastodon_finish', 'unused'))
app.add_url_rule('/publish/twitter/finish', view_func=TwitterSend.as_view('publish_twitter_finish', 'unused'))
