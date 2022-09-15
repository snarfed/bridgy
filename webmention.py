"""Base handler class and common utilities for handling webmentions.

Used in publish.py and blog_webmention.py.

Webmention spec: http://webmention.org/
"""
import logging

from flask import jsonify, request
from flask.views import View
from google.cloud import error_reporting
from oauth_dropins.webutil.util import json_dumps, json_loads
from oauth_dropins.webutil import flask_util
import werkzeug.exceptions

from flask_app import app
import util

logger = logging.getLogger(__name__)


@app.route('/publish/<any(flickr,github,mastodon,twitter):silo>',
           methods=['GET', 'HEAD'])
def webmention_get_or_head(silo):
  """Serves webmention discovery for HEADs to webmention endpoints."""
  return f"""\
<!DOCTYPE html>
<html><head>
<link rel="webmention" href="{util.host_url('/publish/webmention')}">
</head>
<body>Nothing here! <a href="/about">Try the docs instead.</a></body>
</html>""", {
    'Link': f'<{util.host_url("/publish/webmention")}>; rel="webmention"',
  }


class Webmention(View):
  """Webmention base view.

  Attributes:

  * source: the :class:`models.Source` for this webmention
  * entity: the :class:`models.Publish` or :class:`models.Webmention` entity for
    this webmention
  """
  source = None
  entity = None

  def fetch_mf2(self, url, id=None, require_mf2=True, raise_errors=False):
    """Fetches a URL and extracts its mf2 data.

    Side effects: sets :attr:`entity`\ .html on success, calls :attr:`error()`
    on errors.

    Args:
      url: string
      id: string, optional id of specific element to extract and parse. defaults
        to the whole page.
      require_mf2: boolean, whether to return error if no mf2 are found
      raise_errors: boolean, whether to let error exceptions propagate up or
        handle them

    Returns:
      (:class:`requests.Response`, mf2 data dict) on success, None on failure
    """
    try:
      resp = util.requests_get(url)
      resp.raise_for_status()
    except werkzeug.exceptions.HTTPException:
      # raised by us, probably via self.error()
      raise
    except BaseException as e:
      if raise_errors:
        raise
      util.interpret_http_exception(e)  # log exception
      self.error(f'Could not fetch source URL {url}')

    if self.entity:
      self.entity.html = resp.text

    # parse microformats
    soup = util.parse_html(resp)
    mf2 = util.parse_mf2(soup, url=resp.url, id=id)
    if id and not mf2:
      self.error(f'Got fragment {id} but no element found with that id.')

    # special case tumblr's markup: div#content > div.post > div.copy
    # convert to mf2 and re-parse
    if not mf2.get('items'):
      contents = soup.find_all(id='content')
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
          # TODO: i should be able to pass post or contents[0] to mf2py instead
          # here, but it returns no items. mf2py bug?
          doc = str(post)
          mf2 = util.parse_mf2(doc, resp.url)

    logger.debug(f'Parsed microformats2: {json_dumps(mf2, indent=2)}')
    items = mf2.get('items', [])
    if require_mf2 and (not items or not items[0]):
      self.error('No microformats2 data found in ' + resp.url, data=mf2, html=f"""
No <a href="http://microformats.org/get-started">microformats</a> or
<a href="http://microformats.org/wiki/microformats2">microformats2</a> found in
<a href="{resp.url}">{util.pretty_link(resp.url)}</a>! See <a href="http://indiewebify.me/">indiewebify.me</a>
for details (skip to level 2, <em>Publishing on the IndieWeb</em>).
""")

    return resp, mf2

  def error(self, error, html=None, status=400, data=None, log_exception=False,
            report=False, extra_json=None):
    """Handle an error. May be overridden by subclasses.

    Args:
      error: string human-readable error message
      html: string HTML human-readable error message
      status: int HTTP response status code
      data: mf2 data dict parsed from source page
      log_exception: boolean, whether to include a stack trace in the log msg
      report: boolean, whether to report to StackDriver Error Reporting
      extra_json: dict to be merged into the JSON response body
    """
    if self.entity and self.entity.status == 'new':
      self.entity.status = 'failed'
      self.entity.put()

    resp = {'error': error}
    if data:
      resp['parsed'] = data
    if extra_json:
      assert 'error' not in extra_json
      assert 'parsed' not in extra_json
      resp.update(extra_json)

    if report and status != 404:
      self.report_error(error, status=status)

    flask_util.error(str(resp), status=status, response=jsonify(resp),
                     exc_info=log_exception)

  def report_error(self, resp, status=None):
    """Report an error to StackDriver Error reporting."""
    # don't report specific known failures
    if ('Deadline exceeded while waiting for HTTP response' in resp or
        'urlfetch.Fetch() took too long' in resp or
        # WordPress Jetpack bugs
        # https://github.com/snarfed/bridgy/issues/161
        '"resp": "invalid_input"' in resp or
        # https://github.com/snarfed/bridgy/issues/750
        '"error": "jetpack_verification_failed"' in resp or
        # https://console.cloud.google.com/errors/CMjIg52NkMLQYA?project=brid-gy
        'The Jetpack site encountered an error and could not process the API request' in resp or
        # https://console.cloud.google.com/errors/CL6xvLS7k6qE3QE?project=brid-gy
        'The Jetpack site is inaccessible or returned an error' in resp or
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
        'You have already retweeted this' in resp or
        # Facebook duplicate publish attempts
        'This status update is identical to the last one you posted.' in resp or
        # WordPress duplicate comment
        # "error": "Error: 409 HTTP Error 409: Conflict; {\n    \"error\": \"comment_duplicate\",\n    \"message\": \"Duplicate comment detected; it looks as though you&#8217;ve already said that!\"\n}\n"
        'comment_duplicate' in resp):
      return

    subject = '%s %s' % (self.__class__.__name__,
                         '%s %s' % (self.entity.type, self.entity.status)
                         if self.entity else 'failed')
    user = self.source.bridgy_url() if self.source else None
    util.report_error(subject, user=user,
                      http_context=error_reporting.HTTPContext(
                        method=request.method,
                        url=request.url,
                        response_status_code=status,
                        remote_ip=request.remote_addr))
