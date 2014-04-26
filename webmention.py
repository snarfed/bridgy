"""Base handler class and common utilities for handling webmentions.

Used in publish.py and blog_webmention.py.

Webmention spec: http://webmention.org/
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import logging
import json
import sys
import urllib2

import appengine_config
from appengine_config import HTTP_TIMEOUT

from mf2py import parser
import models
import requests
import util
import webapp2


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
<html>""" % self.request.host_url)


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
      fetched = requests.get(url, allow_redirects=True, timeout=HTTP_TIMEOUT)
    except BaseException:
      return self.error('Could not fetch source URL %s' % url)

    if self.entity:
      self.entity.html = fetched.text

    # parse microformats, convert to ActivityStreams
    data = parser.Parser(doc=fetched.text, url=fetched.url).to_dict()
    logging.debug('Parsed microformats2: %s', data)
    items = data.get('items', [])
    if not items or not items[0]:
      return self.error('No microformats2 data found in ' + fetched.url,
                        data=data, html="""
No <a href="http://microformats.org/wiki/microformats2">microformats2</a> data
found in <a href="%s">%s</a>! See <a href="http://indiewebify.me/">indiewebify.me</a>
for details (skip to level 2, <em>Publishing on the IndieWeb</em>).
""" % (fetched.url, util.pretty_link(fetched.url)))

    return fetched, data

  def error(self, error, html=None, status=400, data=None, log_exception=True):
    """Handle an error. May be overridden by subclasses.

    Args:
      error: string human-readable error message
      html: string HTML human-readable error message
      status: int HTTP response status code
      data: mf2 data dict parsed from source page
      log_exception: boolean, whether to include a stack trace in the log msg
    """
    logging.error(error, exc_info=sys.exc_info() if log_exception else None)

    if self.entity:
      self.entity.status = 'failed'
      self.entity.put()

    self.response.set_status(status)
    resp = {'error': error}
    if data:
      resp['parsed'] = data

    resp = json.dumps(resp, indent=2)
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
