"""Converts webmentions to comments on Blogger, Tumblr, and WP.com.
"""
import logging
import urllib.parse

from flask import request
from granary import microformats2
from oauth_dropins.webutil.util import get_first

from flask_app import app
import models
from models import BlogWebmention
import util
import webmention


class BlogWebmentionView(webmention.Webmention):
  """View for incoming webmentions against blog providers."""

  def dispatch_request(self, site):
    logging.info('Params: %s', list(request.values.items()))
    # strip fragments from source and target url
    self.source_url = urllib.parse.urldefrag(request.form['source'])[0]
    self.target_url = urllib.parse.urldefrag(request.form['target'])[0]

    # follow target url through any redirects, strip utm_* query params
    resp = util.follow_redirects(self.target_url)
    redirected_target_urls = [r.url for r in resp.history]
    self.target_url = util.clean_url(resp.url)

    # parse and validate target URL
    domain = util.domain_from_link(self.target_url)
    if not domain:
      self.error('Could not parse target URL %s' % self.target_url)

    # look up source by domain
    source_cls = models.sources[site]
    domain = domain.lower()
    self.source = (source_cls.query()
                   .filter(source_cls.domains == domain)
                   .filter(source_cls.features == 'webmention')
                   .filter(source_cls.status == 'enabled')
                   .get())
    if not self.source:
      # check for a rel-canonical link. Blogger uses these when it serves a post
      # from multiple domains, e.g country TLDs like epeus.blogspot.co.uk vs
      # epeus.blogspot.com.
      # https://github.com/snarfed/bridgy/issues/805
      mf2 = self.fetch_mf2(self.target_url, require_mf2=False)
      if not mf2:
        # fetch_mf2() already wrote the error response
        return
      domains = util.dedupe_urls(
        util.domain_from_link(url)
        for url in mf2[1]['rels'].get('canonical', []))
      if domains:
        self.source = (source_cls.query()
                       .filter(source_cls.domains.IN(domains))
                       .filter(source_cls.features == 'webmention')
                       .filter(source_cls.status == 'enabled')
                       .get())

    if not self.source:
      self.error(
        'Could not find %s account for %s. Is it registered with Bridgy?' %
        (source_cls.GR_CLASS.NAME, domain))

    # check that the target URL path is supported
    target_path = urllib.parse.urlparse(self.target_url).path
    if target_path in ('', '/'):
      msg = 'Home page webmentions are not currently supported.'
      logging.info(msg)
      return {'error': msg}, 202
    for pattern in self.source.PATH_BLOCKLIST:
      if pattern.match(target_path):
        msg = f'{self.source.GR_CLASS.NAME} webmentions are not supported for URL path: {target_path}'
        logging.info(msg)
        return {'error': msg}, 202

    # create BlogWebmention entity
    id = '%s %s' % (self.source_url, self.target_url)
    self.entity = BlogWebmention.get_or_insert(
      id, source=self.source.key, redirected_target_urls=redirected_target_urls)
    if self.entity.status == 'complete':
      # TODO: response message saying update isn't supported
      return self.entity.published
    logging.debug("BlogWebmention entity: '%s'", self.entity.key.urlsafe().decode())

    # fetch source page
    fetched = self.fetch_mf2(self.source_url)
    if not fetched:
      return
    resp, mf2 = fetched

    item = self.find_mention_item(mf2.get('items', []))
    if not item:
      self.error('Could not find target URL %s in source page %s' %
                 (self.target_url, resp.url), data=mf2, log_exception=False)

    # default author to target domain
    author_name = domain
    author_url = 'http://%s/' % domain

    # extract author name and URL from h-card, if any
    props = item['properties']
    author = get_first(props, 'author')
    if author:
      if isinstance(author, str):
        author_name = author
      else:
        author_props = author.get('properties', {})
        author_name = get_first(author_props, 'name')
        author_url = get_first(author_props, 'url')

    # if present, u-url overrides source url
    u_url = get_first(props, 'url')
    if u_url:
      self.entity.u_url = u_url

    # generate content
    content = props['content'][0]  # find_mention_item() guaranteed this is here
    text = (content.get('html') or content.get('value')).strip()
    source_url = self.entity.source_url()
    text += ' <br /> <a href="%s">via %s</a>' % (
      source_url, util.domain_from_link(source_url))

    # write comment
    try:
      self.entity.published = self.source.create_comment(
        self.target_url, author_name, author_url, text)
    except Exception as e:
      code, body = util.interpret_http_exception(e)
      msg = 'Error: %s: %s; %s' % (code, e, body)
      if code == '401':
        logging.warning(f'Disabling source due to: {e}', exc_info=True)
        self.source.status = 'disabled'
        self.source.put()
        self.error(msg, status=code, report=self.source.is_beta_user())
      elif code == '404':
        # post is gone
        self.error(msg, status=code, report=False)
      elif util.is_connection_failure(e) or (code and int(code) // 100 == 5):
        self.error(msg, status=502, report=False)
      elif code or body:
        self.error(msg, status=code, report=True)
      else:
        raise

    # write results to datastore
    self.entity.status = 'complete'
    self.entity.put()

    return self.entity.published

  def find_mention_item(self, items):
    """Returns the mf2 item that mentions (or replies to, likes, etc) the target.

    May modify the items arg, e.g. may set or replace content.html or
    content.value.

    Args:
      items: sequence of mf2 item dicts

    Returns:
      mf2 item dict or None
    """
    # find target URL in source
    for item in items:
      props = item.setdefault('properties', {})

      # find first non-empty content element
      content = props.setdefault('content', [{}])[0]
      text = content.get('html') or content.get('value')

      for type in 'in-reply-to', 'like', 'like-of', 'repost', 'repost-of':
        urls = [urllib.parse.urldefrag(u)[0] for u in
                microformats2.get_string_urls(props.get(type, []))]
        if self.any_target_in(urls):
          break
      else:
        if text and self.any_target_in(text):
          type = 'post'
          url = get_first(props, 'url') or self.source_url
          name = get_first(props, 'name') or get_first(props, 'summary')
          text = content['html'] = ('mentioned this in %s.' %
                                    util.pretty_link(url, text=name, max_length=280))
        else:
          type = None

      if type:
        # found the target!
        rsvp = get_first(props, 'rsvp')
        if rsvp:
          self.entity.type = 'rsvp'
          if not text:
            content['value'] = 'RSVPed %s.' % rsvp
        else:
          self.entity.type = {'in-reply-to': 'comment',
                              'like-of': 'like',
                              'repost-of': 'repost',
                              }.get(type, type)
          if not text:
            content['value'] = {'comment': 'replied to this.',
                                'like': 'liked this.',
                                'repost': 'reposted this.',
                                }[self.entity.type]
        return item

      # check children in case this is eg an h-feed
      found = self.find_mention_item(item.get('children', []))
      if found:
        return found

    return None

  def any_target_in(self, haystack):
    """Returns true if any target URL (including redirects) is in haystack."""
    return any(
        target in haystack
        for target in self.entity.redirected_target_urls + [self.target_url])


app.add_url_rule('/webmention/<any(blogger,fake,tumblr,wordpress):site>',
                 view_func=BlogWebmentionView.as_view('blog_wm'), methods=['POST'])
