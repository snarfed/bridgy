"""Base handler class and common utilities for handling webmentions.

Used in publish.py and blog_webmention.py.

Webmention spec: http://webmention.org/
"""
from __future__ import unicode_literals

import logging
import json

import appengine_config

import util


class WebmentionGetHandler(util.Handler):
  """Renders a simple placeholder HTTP page for GETs to webmention endpoints.
  """
  def head(self, site=None):
    self.response.headers['Link'] = (
      '<%s/publish/webmention>; rel="webmention"' % self.request.host_url)

  @util.canonicalize_domain
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

  * source: the :class:`models.Source` for this webmention
  * entity: the :class:`models.Publish` or :class:`models.Webmention` entity for
    this webmention
  """
  source = None
  entity = None

  def fetch_mf2(self, url):
    """Fetches a URL and extracts its mf2 data.

    Side effects: sets :attr:`entity`\ .html on success, calls :attr:`error()`
    on errors.

    Args:
      url: string

    Returns:
      (:class:`requests.Response`, mf2 data dict) on success, None on failure
    """
    try:
      fetched = util.requests_get(url)
      fetched.raise_for_status()
    except BaseException as e:
      util.interpret_http_exception(e)  # log exception
      return self.error('Could not fetch source URL %s' % url)

    if self.entity:
      self.entity.html = fetched.text

    # .text is decoded unicode string, .content is raw bytes. if the HTTP
    # headers didn't specify a charset, pass raw bytes to BeautifulSoup so it
    # can look for a <meta> tag with a charset and decode.
    text = (fetched.text if 'charset' in fetched.headers.get('content-type', '')
            else fetched.content)
    doc = util.beautifulsoup_parse(text)

    # parse microformats
    data = util.mf2py_parse(doc, fetched.url)

    # special case tumblr's markup: div#content > div.post > div.copy
    # convert to mf2 and re-parse
    if not data.get('items'):
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
          data = util.mf2py_parse(doc, fetched.url)

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
    logging.info(error, exc_info=log_exception)

    if self.entity:
      self.entity.status = 'failed'
      self.entity.put()

    self.response.set_status(status)
    resp = {'error': error}
    if data:
      resp['parsed'] = data
    resp = json.dumps(resp, indent=2)

    if mail and status != 404:
      self.mail_me('[Returned HTTP %s to client]\n\n%s' % (status, error))
    self.response.write(resp)

  def mail_me(self, resp):
    # don't email about specific known failures
    if ('Deadline exceeded while waiting for HTTP response' in resp or
        'urlfetch.Fetch() took too long' in resp or
        # WordPress Jetpack bugs
        # https://github.com/snarfed/bridgy/issues/161
        '"resp": "invalid_input"' in resp or
        # https://github.com/snarfed/bridgy/issues/750
        '"error": "jetpack_verification_failed"' in resp or
        # Blogger known bug
        # https://github.com/snarfed/bridgy/issues/175
        'bX-2i87au' in resp or
        # Tumblr: transient Disqus error looking up thread
        # https://github.com/snarfed/bridgy/issues/177
        "Invalid argument, 'thread': Unable to find thread" in resp or
        # expected for partially set up tumblr accounts
        "we haven't found your Disqus account" in resp or
        # Twitter 5MB image file size limit
        '"message":"Image file size must be' in resp or
        # Twitter media file number limits
        'Tweet with media must have exactly 1 gif or video' in resp or
        # Facebook image type/size req'ts
        'Missing or invalid image file' in resp or
        "Your photos couldn't be uploaded. Photos should be less than 4 MB" in resp or
        # Twitter duplicate publish attempts
        'Status is a duplicate.' in resp or
        'You have already favorited this status.' in resp or
        # Facebook duplicate publish attempts
        'This status update is identical to the last one you posted.' in resp or
        # WordPress duplicate comment
        # "error": "Error: 409 HTTP Error 409: Conflict; {\n    \"error\": \"comment_duplicate\",\n    \"message\": \"Duplicate comment detected; it looks as though you&#8217;ve already said that!\"\n}\n"
        'comment_duplicate' in resp):
      return

    subject = '%s %s' % (self.__class__.__name__,
                         '%s %s' % (self.entity.type, self.entity.status)
                         if self.entity else 'failed')
    body = 'Request:\n%s\n\nResponse:\n%s' % (self.request.params.items(), resp)

    if self.source:
      body = 'Source: %s\n\n%s' % (self.source.bridgy_url(self), body)
      subject += ': %s' % self.source.label()

    util.email_me(subject=subject, body=body)
