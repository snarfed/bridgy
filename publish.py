"""Publishes webmentions into the silos.

Webmention spec: http://webmention.org/

Bridgy request and response details: https://brid.gy/about#response

Example request::

    POST /webmention HTTP/1.1
    Host: brid.gy
    Content-Type: application/x-www-url-form-encoded

    source=http://bob.host/post-by-bob&
    target=http://facebook.com/123

Example response::

    HTTP/1.1 201 Created
    Location: http://facebook.com/456_789

    {
      "url": "http://facebook.com/456_789",
      "type": "post",
      "id": "456_789"
    }
"""
from __future__ import absolute_import, unicode_literals
from future import standard_library
standard_library.install_aliases()
from future.utils import native_str

import collections
import logging
import pprint
import re
import urllib.request, urllib.parse, urllib.error

import appengine_config

from google.cloud import ndb
from granary import microformats2
from granary import source as gr_source
from oauth_dropins import (
  facebook as oauth_facebook,
  flickr as oauth_flickr,
  github as oauth_github,
  mastodon as oauth_mastodon,
  twitter as oauth_twitter,
)
from oauth_dropins.webutil.handlers import JINJA_ENV
from oauth_dropins.webutil.util import json_dumps, json_loads
import webapp2

from facebook import FacebookPage
from flickr import Flickr
from github import GitHub
from instagram import Instagram
from mastodon import Mastodon
from models import Publish, PublishedPage
from twitter import Twitter
import models
import util
import webmention


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


class Handler(webmention.WebmentionHandler):
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
    return util.get_required_param(self, 'source').strip()

  def target_url(self):
    return util.get_required_param(self, 'target').strip()

  def include_link(self, item):
    val = self.request.get('bridgy_omit_link', None)

    if val is None:
      # _run has already parsed and validated the target URL
      vals = urllib.parse.parse_qs(urllib.parse.urlparse(self.target_url()).query)\
                     .get('bridgy_omit_link')
      val = vals[0] if vals else None

    if val is None:
      vals = item.get('properties', {}).get('bridgy-omit-link')
      val = vals[0] if vals else None

    result = (gr_source.INCLUDE_LINK if val is None or val.lower() == 'false'
              else gr_source.INCLUDE_IF_TRUNCATED if val.lower() == 'maybe'
              else gr_source.OMIT_LINK)

    return result

  def ignore_formatting(self, item):
    val = self.request.get('bridgy_ignore_formatting', None)

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
    silo_content = props.get('bridgy-%s-content' % self.source.SHORT_NAME, [])
    if silo_content:
      props['content'] = silo_content
      props.pop('name', None)
      props.pop('summary', None)

  def _run(self):
    """Returns CreationResult on success, None otherwise."""
    logging.info('Params: %s', self.request.params.items())
    assert self.PREVIEW in (True, False)

    # parse and validate target URL
    try:
      parsed = urllib.parse.urlparse(self.target_url())
    except BaseException:
      return self.error('Could not parse target URL %s' % self.target_url())

    domain = parsed.netloc
    path_parts = parsed.path.rsplit('/', 1)
    source_cls = SOURCE_NAMES.get(path_parts[-1])
    if (domain not in util.DOMAINS or
        len(path_parts) != 2 or path_parts[0] != '/publish' or not source_cls):
      return self.error(
        'Target must be brid.gy/publish/{flickr,github,mastodon,twitter}')
    elif source_cls == Instagram:
      return self.error('Sorry, %s is not supported.' %
                        source_cls.GR_CLASS.NAME)

    # resolve source URL
    url, domain, ok = util.get_webmention_target(
      self.source_url(), replace_test_domains=False)
    # show nice error message if they're trying to publish a silo post
    if domain in SOURCE_DOMAINS:
      return self.error(
        "Looks like that's a %s URL. Try one from your web site instead!" %
        SOURCE_DOMAINS[domain].GR_CLASS.NAME)
    elif not ok:
      return self.error('Unsupported source URL %s' % url)
    elif not domain:
      return self.error('Could not parse source URL %s' % url)

    # look up source by domain
    self.source = self._find_source(source_cls, url, domain)
    if not self.source:
      return  # _find_source rendered the error

    content_param = 'bridgy_%s_content' % self.source.SHORT_NAME
    if content_param in self.request.params:
      return self.error('The %s parameter is not supported' % content_param)

    # show nice error message if they're trying to publish their home page
    for domain_url in self.source.domain_urls:
      domain_url_parts = urllib.parse.urlparse(domain_url)
      for source_url in url, self.source_url():
        parts = urllib.parse.urlparse(source_url)
        if (parts.netloc == domain_url_parts.netloc and
            parts.path.strip('/') == domain_url_parts.path.strip('/') and
            not parts.query):
          return self.error(
            "Looks like that's your home page. Try one of your posts instead!")

    # done with the sanity checks, ready to fetch the source url. create the
    # Publish entity so we can store the result.
    self.entity = self.get_or_add_publish_entity(url)
    try:
      resp = self.fetch_mf2(url, raise_errors=True)
    except BaseException as e:
      status, body = util.interpret_http_exception(e)
      if status == '410':
        return self.delete(url)
      return self.error('Could not fetch source URL %s' % url)

    if not resp:
      return
    self.fetched, mf2 = resp

    # create the Publish entity so we can store the result.
    if (self.entity.status == 'complete' and self.entity.type != 'preview' and
        not self.PREVIEW and not appengine_config.DEBUG):
      return self.error("Sorry, you've already published that page, and Bridgy Publish doesn't support updating existing posts. Details: https://github.com/snarfed/bridgy/issues/84",
                        extra_json={'original': self.entity.published})

    # find rel-shortlink, if any
    # http://microformats.org/wiki/rel-shortlink
    # https://github.com/snarfed/bridgy/issues/173
    shortlinks = mf2['rels'].get('shortlink')
    if shortlinks:
      self.shortlink = urllib.parse.urljoin(url, shortlinks[0])

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
      except BaseException as e:
        code, body = util.interpret_http_exception(e)
        if code in self.source.DISABLE_HTTP_CODES or isinstance(e, models.DisableSource):
          # the user deauthorized the bridgy app, or the token expired, so
          # disable this source.
          logging.warning('Disabling source due to: %s' % e, exc_info=True)
          self.source.status = 'disabled'
          self.source.put()
          # util.email_me(subject='Bridgy Publish: disabled %s' % self.source.label(),
          #               body=body)
        if isinstance(e, (NotImplementedError, ValueError, urllib.error.URLError)):
          code = '400'
        elif not code:
          raise
        msg = 'Error: %s %s' % (body or '', e)
        return self.error(msg, status=code, report=code not in ('400', '404', '502', '503', '504'))

    if not self.entity.published:  # tried all the items
      types.discard('h-entry')
      types.discard('h-note')
      if types:
        msg = ("%s doesn't support type(s) %s, or no content was found." %
               (source_cls.GR_CLASS.NAME, ' + '.join(types)))
      else:
        msg = 'Could not find content in <a href="http://microformats.org/wiki/h-entry">h-entry</a> or any other element!'
      return self.error(msg, data=mf2)

    # write results to datastore
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
    sources = source_cls.query().filter(source_cls.domains == domain).fetch(100)
    if not sources:
      self.error("Could not find <b>%(type)s</b> account for <b>%(domain)s</b>. Check that your %(type)s profile has %(domain)s in its <em>web site</em> or <em>link</em> field, then try signing up again." %
        {'type': source_cls.GR_CLASS.NAME, 'domain': domain})
      return

    current_url = ''
    sources_ready = []
    best_match = None
    for source in sources:
      logging.info('Source: %s , features %s, status %s, poll status %s',
                   source.bridgy_url(self), source.features, source.status,
                   source.poll_status)
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
    elif sources_ready:
      self.error(
        'No account found that matches %s. Check that <a href="%s/about#profile-link">the web site URL is in your silo profile</a>, then <a href="%s/">sign up again</a>.' %
        (self.request.host_url, util.pretty_link(url), self.request.host_url))
    else:
      self.error('Publish is not enabled for your account. <a href="%s/">Try signing up!</a>' % self.request.host_url)

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
    logging.debug('Converted to ActivityStreams object: %s', json_dumps(obj, indent=2))

    # posts and comments need content
    obj_type = obj.get('objectType')
    if obj_type in ('note', 'article', 'comment'):
      if (not obj.get('content') and not obj.get('summary') and
          not obj.get('displayName')):
        return gr_source.creation_result(
          abort=False,
          error_plain='Could not find content in %s' % self.fetched.url,
          error_html='Could not find <a href="http://microformats.org/">content</a> in %s' % self.fetched.url)

    self.preprocess(obj)

    include_link = self.include_link(item)

    if not self.authorize():
      return gr_source.creation_result(abort=True)

    # RIP Facebook.
    # https://github.com/snarfed/bridgy/issues/817
    # https://github.com/snarfed/bridgy/issues/350
    if isinstance(self.source, FacebookPage):
      return gr_source.creation_result(
        abort=True,
        error_plain='Facebook is no longer supported. So long, and thanks for all the fish!',
        error_html='<a href="https://brid.gy/about#rip-facebook">Facebook is no longer supported. So long, and thanks for all the fish!</a>')

    if self.PREVIEW:
      result = self.source.gr_source.preview_create(
        obj, include_link=include_link, ignore_formatting=ignore_formatting)
      self.entity.published = result.content or result.description
      if not self.entity.published:
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
      self.response.headers['Content-Type'] = 'application/json'
      logging.info('Returning %s', json_dumps(self.entity.published, indent=2))
      self.response.headers['Location'] = self.entity.published['url'].encode('utf-8')
      self.response.status = 201
      return gr_source.creation_result(
        json_dumps(self.entity.published, indent=2))

  def delete(self, source_url):
    """Attempts to delete or preview delete a published post.

    Args:
      source_url: string, original post URL

    Returns:
      dict response data with at least id and url
    """
    self.entity = self.get_or_add_publish_entity(source_url)
    if ((self.entity.status != 'complete' or self.entity.type == 'preview') and
        not appengine_config.DEBUG):
      return self.error("Can't delete this post from %s because Bridgy Publish didn't originally POSSE it there" % self.source.gr_source.NAME)

    id = self.entity.published.get('id')
    url = self.entity.published.get('url')
    if not id and url:
      id = self.source.gr_source.post_id(url)

    if not id:
      return self.error(
        "Bridgy Publish can't find the id of the %s post that it originally published for %s" %
        self.source.gr_source.NAME, source_url)

    if self.PREVIEW:
      try:
        return self._render_preview(self.source.gr_source.preview_delete(id))
      except NotImplementedError:
        return self.error("Sorry, deleting isn't supported for %s yet" %
                          self.source.gr_source.NAME)

    logging.info('Deleting silo post id %s', id)
    self.entity = models.Publish(parent=self.entity.key.parent(),
                                 source=self.source.key, type='delete')
    self.entity.put()
    logging.debug("Publish entity for delete: '%s'", self.entity.key.urlsafe())

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

        logging.debug('expand_target_urls fetching field=%s, url=%s', field, url)
        try:
          mf2 = util.fetch_mf2(url)
        except AssertionError:
          raise  # for unit tests
        except BaseException:
          # it's not a big deal if we can't fetch an in-reply-to url
          logging.info('expand_target_urls could not fetch field=%s, url=%s',
                       field, url, exc_info=True)
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

        logging.debug('expand_target_urls found rel=syndication for url=%s: %r', url, synd_urls)
        augmented += [{'url': u} for u in synd_urls]

      activity[field] = augmented

  @ndb.transactional
  def get_or_add_publish_entity(self, source_url):
    """Creates and stores :class:`models.Publish` entity.

    ...and if necessary, :class:`models.PublishedPage` entity.

    Args:
      source_url: string
    """
    page = PublishedPage.get_or_insert(native_str(source_url.encode('utf-8')))
    entity = Publish.query(
      Publish.status == 'complete', Publish.type != 'preview',
      Publish.source == self.source.key,
      ancestor=page.key).get()

    if entity is None:
      entity = Publish(parent=page.key, source=self.source.key)
      if self.PREVIEW:
        entity.type = 'preview'
      entity.put()

    logging.debug("Publish entity: '%s'", entity.key.urlsafe())
    return entity

  def _render_preview(self, result, include_link=False):
    """Renders a preview CreationResult as HTML.

    Args:
      result: CreationResult
      include_link: boolean

    Returns: CreationResult with the rendered HTML in content
    """
    state = {
      'source_key': self.source.key.urlsafe(),
      'source_url': self.source_url(),
      'target_url': self.target_url(),
      'include_link': include_link,
    }
    vars = {
      'source': self.preprocess_source(self.source),
      'preview': result.content,
      'description': result.description,
      'webmention_endpoint': util.host_url(self) + '/publish/webmention',
      'state': util.encode_oauth_state(state),
    }
    vars.update(state)
    logging.info('Rendering preview with template vars %s', pprint.pformat(vars))
    return gr_source.creation_result(
      JINJA_ENV.get_template('preview.html').render(**vars))


class PreviewHandler(Handler):
  """Renders a preview HTML snippet of how a webmention would be handled.
  """
  PREVIEW = True

  def post(self):
    result = self._run()
    if result and result.content:
      self.response.write(result.content)

  def authorize(self):
    from_source = self.load_source()
    if from_source.key != self.source.key:
      self.error('Try publishing that page from <a href="%s">%s</a> instead.' %
                 (self.source.bridgy_path(), self.source.label()))
      return False

    return True

  def include_link(self, item):
    # always use query param because there's a checkbox in the UI
    val = self.request.get('bridgy_omit_link', None)
    return (gr_source.INCLUDE_LINK if val is None or val.lower() == 'false'
            else gr_source.INCLUDE_IF_TRUNCATED if val.lower() == 'maybe'
            else gr_source.OMIT_LINK)

  def error(self, error, html=None, status=400, data=None, report=False, **kwargs):
    logging.info(error, exc_info=True)
    self.response.set_status(status)
    error = html if html else util.linkify(error)
    self.response.write(error)
    if report:
      self.report_error(error)


class SendHandler(Handler):
  """Interactive publish handler. Redirected to after each silo's OAuth dance.

  Note that this is GET, not POST, since HTTP redirects always GET.
  """
  PREVIEW = False

  def finish(self, auth_entity, state=None):
    self.state = util.decode_oauth_state(state)
    if not state:
      self.error('If you want to publish or preview, please approve the prompt.')
      return self.redirect('/')

    source = ndb.Key(urlsafe=self.state['source_key']).get()
    if auth_entity is None:
      self.error('If you want to publish or preview, please approve the prompt.')
    elif not auth_entity.is_authority_for(source.auth_entity):
      self.error('Please log into %s as %s to publish that page.' %
                 (source.GR_CLASS.NAME, source.name))
    else:
      result = self._run()
      if result and result.content:
        self.messages.add('Done! <a href="%s">Click here to view.</a>' %
                          self.entity.published.get('url'))
        granary_message = self.entity.published.get('granary_message')
        if granary_message:
          self.messages.add(granary_message)
      # otherwise error() added an error message

    return self.redirect(source.bridgy_url(self))

  def source_url(self):
    return self.state['source_url']

  def target_url(self):
    return self.state['target_url']

  def include_link(self, item):
    return self.state['include_link']

  def error(self, error, html=None, status=400, data=None, report=False, **kwargs):
    logging.info(error, exc_info=True)
    error = html if html else util.linkify(error)
    self.messages.add('%s' % error)
    if report:
      self.report_error(error)


# We want CallbackHandler.get() and SendHandler.finish(), so put
# CallbackHandler first and override finish.
class FacebookSendHandler(oauth_facebook.CallbackHandler, SendHandler):
  finish = SendHandler.finish


class FlickrSendHandler(oauth_flickr.CallbackHandler, SendHandler):
  finish = SendHandler.finish


class GitHubSendHandler(oauth_github.CallbackHandler, SendHandler):
  finish = SendHandler.finish


class MastodonSendHandler(oauth_mastodon.CallbackHandler, SendHandler):
  finish = SendHandler.finish


class TwitterSendHandler(oauth_twitter.CallbackHandler, SendHandler):
  finish = SendHandler.finish


class WebmentionHandler(Handler):
  """Accepts webmentions and translates them to publish requests.
  """
  PREVIEW = False

  def post(self):
    result = self._run()
    if result:
      self.response.write(result.content)

  def authorize(self):
    """Check for a backlink to brid.gy/publish/SILO."""
    bases = set()
    if util.domain_from_link(self.request.host_url) == 'brid.gy':
      bases.add('brid.gy')
      bases.add('www.brid.gy')  # also accept www
    else:
      bases.add(self.request.host_url)

    expected = ['%s/publish/%s' % (base, self.source.SHORT_NAME) for base in bases]

    if self.entity.html:
      for url in expected:
        if url in self.entity.html or urllib.parse.quote(url, safe='') in self.entity.html:
          return True

    self.error("Couldn't find link to %s" % expected[0])
    return False


application = webapp2.WSGIApplication([
    ('/publish/preview', PreviewHandler),
    ('/publish/webmention', WebmentionHandler),
    ('/publish/(facebook|flickr|github|mastodon|twitter)',
     webmention.WebmentionGetHandler),
    ('/publish/flickr/finish', FlickrSendHandler),
    ('/publish/github/finish', GitHubSendHandler),
    ('/publish/mastodon/finish', MastodonSendHandler),
    ('/publish/twitter/finish', TwitterSendHandler),
    ],
  debug=appengine_config.DEBUG)
