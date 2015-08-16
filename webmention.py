"""Base handler class and common utilities for handling webmentions.

Used in publish.py and blog_webmention.py.

Webmention spec: http://webmention.org/
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import logging
import json
import pprint

import appengine_config
from appengine_config import HTTP_TIMEOUT

from bs4 import BeautifulSoup
from mf2py import parser
import requests
import util


class WebmentionGetHandler(util.Handler):
  """Renders a simple placeholder HTTP page for GETs to webmention endpoints.
  """
  def head(self, site=None):
    self.response.headers['Link'] = (
      '<%s/publish/webmention>; rel="webmention"' % self.request.host_url)

  def get(self, site=None):
    self.head(site)
    self.response.out.write("""\
<!DOCTYPE html>
<html><head>
<link rel="webmention" href="%s/publish/webmention">
</head>
<body>Nothing here! <a href="/about">Try the docs instead.</a></body>
</html>""" % self.request.host_url)


class WebmentionHandler(WebmentionGetHandler):
  """Webmention handler.

  Attributes:
    source: the Source for this webmention
    entity: the Publish or Webmention entity for this webmention
  """
  source = None
  entity = None

  def fetch_mf2(self, url):
    """Fetches a URL and extracts its mf2 data.

    Side effects: sets self.entity.html on success, calls self.error() on
    errors.

    Args:
      url: string

    Returns:
      (requests.Response, mf2 data dict) on success, None on failure
    """
    try:
      fetched = util.requests_get(url)
      fetched.raise_for_status()
    except BaseException:
      return self.error('Could not fetch source URL %s' % url)

    if self.entity:
      self.entity.html = fetched.text

    # .text is decoded unicode string, .content is raw bytes. if the HTTP
    # headers didn't specify a charset, pass raw bytes to BeautifulSoup so it
    # can look for a <meta> tag with a charset and decode.
    text = (fetched.text if 'charset' in fetched.headers.get('content-type', '')
            else fetched.content)
    doc = BeautifulSoup(text)

    # special case tumblr's markup: div#content > div.post > div.copy
    # convert to mf2.
    contents = doc.find_all(id='content')
    if contents:
      post = contents[0].find_next(class_='post')
      if post:
        post['class'] = 'h-entry'
        copy = post.find_next(class_='copy')
        if copy:
          copy['class'] = 'e-content'
        photo = post.find_next(class_='photo-wrapper')
        if photo:
          img = photo.find_next('img')
          if img:
            img['class'] = 'u-photo'
        doc = unicode(post)

    # parse microformats, convert to ActivityStreams
    data = parser.Parser(doc=doc, url=fetched.url).to_dict()
    logging.debug('Parsed microformats2: %s', json.dumps(data, indent=2))
    items = data.get('items', [])
    if not items or not items[0]:
      return self.error('No microformats2 data found in ' + fetched.url,
                        data=data, html="""
No <a href="http://microformats.org/get-started">microformats</a> or
<a href="http://microformats.org/wiki/microformats2">microformats2</a> found in
<a href="%s">%s</a>! See <a href="http://indiewebify.me/">indiewebify.me</a>
for details (skip to level 2, <em>Publishing on the IndieWeb</em>).
""" % (fetched.url, util.pretty_link(fetched.url)))

    return fetched, data

  def error(self, error, html=None, status=400, data=None, log_exception=True,
            mail=False):
    """Handle an error. May be overridden by subclasses.

    Args:
      error: string human-readable error message
      html: string HTML human-readable error message
      status: int HTTP response status code
      data: mf2 data dict parsed from source page
      log_exception: boolean, whether to include a stack trace in the log msg
      mail: boolean, whether to email me
    """
    logging.warning(error, exc_info=log_exception)

    if self.entity:
      self.entity.status = 'failed'
      self.entity.put()

    self.response.set_status(status)
    resp = {'error': error}
    if data:
      resp['parsed'] = data
    resp = json.dumps(resp, indent=2)

    # don't email about specific known failures
    if (mail and
        'Deadline exceeded while waiting for HTTP response' not in error and
        'urlfetch.Fetch() took too long' not in error and
        # https://github.com/snarfed/bridgy/issues/161
        '"error": "invalid_input"' not in error and
        # https://github.com/snarfed/bridgy/issues/175
        'bX-2i87au' not in error and
        # https://github.com/snarfed/bridgy/issues/177
        "Invalid argument, 'thread': Unable to find thread" not in error and
        # expected for partially set up tumblr accounts
        "we haven't found your Disqus account" not in error
        ):
      self.mail_me(resp)
    self.response.write(resp)

  def mail_me(self, resp):
    subject = '%s %s' % (self.__class__.__name__,
                         '%s %s' % (self.entity.type, self.entity.status)
                         if self.entity else 'failed')
    body = 'Request:\n%s\n\nResponse:\n%s' % (self.request.params.items(), resp)

    if self.source:
      body = 'Source: %s\n\n%s' % (self.source.bridgy_url(self), body)
      subject += ': %s' % self.source.label()

    util.email_me(subject=subject, body=body)
