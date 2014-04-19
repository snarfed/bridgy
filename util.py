# coding=utf-8
"""Misc utility constants and classes.
"""

import urllib
import urlparse

import requests
import webapp2

from mf2py.parser import Parser as Mf2Parser

from activitystreams.oauth_dropins.webutil.util import *
from activitystreams import source
from appengine_config import HTTP_TIMEOUT, DEBUG

from google.appengine.api import mail
from google.appengine.api import memcache
from google.appengine.api import taskqueue
from google.appengine.ext import ndb

EPOCH = datetime.datetime.utcfromtimestamp(0)
POLL_TASK_DATETIME_FORMAT = '%Y-%m-%d-%H-%M-%S'
FAILED_RESOLVE_URL_CACHE_TIME = 60 * 60 * 24  # a day

# Known domains that don't support webmentions. Mainly just the silos.
WEBMENTION_BLACKLIST = {
  'amzn.com',
  'amazon.com',
  'brid.gy',
  'brid-gy.appspot.com',
  'facebook.com',
  'm.facebook.com',
  'instagr.am',
  'instagram.com',
  'plus.google.com',
  'twitter.com',
  # these come from the text of tweets. we also pull the expanded URL
  # from the tweet entities, so ignore these instead of resolving them.
  't.co',
  'youtube.com',
  'youtu.be',
  '', None,
  # individual web sites that fail to fetch on app engine
  'djtymenathanscot.com',
  }


def add_poll_task(source, **kwargs):
  """Adds a poll task for the given source entity.
  """
  last_polled_str = source.last_polled.strftime(POLL_TASK_DATETIME_FORMAT)
  taskqueue.add(queue_name='poll',
                params={'source_key': source.key.urlsafe(),
                        'last_polled': last_polled_str},
                **kwargs)


def add_propagate_task(response, **kwargs):
  """Adds a propagate task for the given response entity.
  """
  taskqueue.add(queue_name='propagate',
                params={'response_key': response.key.urlsafe()},
                # tasks inserted from a backend (e.g. twitter_streaming) are
                # sent to that backend by default, which doesn't work in the
                # dev_appserver. setting the target version to 'default' in
                # queue.yaml doesn't work either, but setting it here does.
                #
                # (note the constant. the string 'default' works in
                # dev_appserver, but routes to default.brid-gy.appspot.com in
                # prod instead of www.brid.gy, which breaks SSL because
                # appspot.com doesn't have a third-level wildcard cert.)
                target=taskqueue.DEFAULT_APP_VERSION)


def email_me(**kwargs):
  """Thin wrapper around mail.send_mail() that handles errors."""
  try:
    mail.send_mail(sender='admin@brid-gy.appspotmail.com',
                   to='webmaster@brid.gy', **kwargs)
  except BaseException:
    logging.exception('Error sending notification email')


def follow_redirects(url):
  """Fetches a URL, follows redirects, and returns the final response.

  Caches resolved URLs in memcache.

  Args:
    url: string

  Returns:
    requests.Response
  """
  cache_key = 'R ' + url
  resolved = memcache.get(cache_key)
  if resolved is not None:
    return resolved

  # can't use urllib2 since it uses GET on redirect requests, even if i specify
  # HEAD for the initial request.
  # http://stackoverflow.com/questions/9967632
  try:
    # default scheme to http
    parsed = urlparse.urlparse(url)
    if not parsed.scheme:
      url = 'http://' + url
    resolved = requests.head(url, allow_redirects=True, timeout=HTTP_TIMEOUT)
    cache_time = 0  # forever
  except BaseException, e:
    logging.warning("Couldn't resolve URL %s : %s", url, e)
    resolved = requests.Response()
    resolved.url = url
    resolved.headers['content-type'] = 'text/html'
    resolved.status_code = 499  # not standard. i made this up.
    cache_time = FAILED_RESOLVE_URL_CACHE_TIME

  refresh = resolved.headers.get('refresh')
  if refresh:
    for part in refresh.split(';'):
      if part.strip().startswith('url='):
        return follow_redirects(part.strip()[4:])

  memcache.set(cache_key, resolved, time=cache_time)
  return resolved


# Wrap webutil.util.tag_uri and hard-code the year to 2013.
#
# Needed because I originally generated tag URIs with the current year, which
# resulted in different URIs for the same objects when the year changed. :/
from activitystreams.oauth_dropins.webutil import util
_orig_tag_uri = tag_uri
util.tag_uri = lambda domain, name: _orig_tag_uri(domain, name, year=2013)


def get_webmention_target(url):
  """Resolves a URL and decides whether we should try to send it a webmention.

  Returns: (string url, string pretty domain, boolean) tuple. The boolean is
    True if we should send a webmention, False otherwise, e.g. if it 's a bad
    URL, not text/html, in the blacklist, or can't be fetched.
  """
  try:
    domain = domain_from_link(url)
  except BaseException, e:
    logging.warning('Dropping bad URL %s.', url)
    return (url, None, False)

  if domain in WEBMENTION_BLACKLIST:
    return (url, domain, False)

  resolved = follow_redirects(url)
  if resolved.url != url:
    logging.debug('Resolved %s to %s', url, resolved.url)
    url = resolved.url
    domain = domain_from_link(url)

  is_html = resolved.headers.get('content-type', '').startswith('text/html')
  return (url, domain, is_html)


def prune_activity(activity):
  """Returns an activity dict with just id, url, content, to, and object.

  If the object field exists, it's pruned down to the same fields. Any fields
  duplicated in both the activity and the object are removed from the object.

  Note that this only prunes the to field if it says the activity is public,
  since activitystreams.Source.is_public() defaults to saying an activity is
  public if the to field is missing. If that ever changes, we'll need to
  start preserving the to field here.

  Args:
    activity: ActivityStreams activity dict

  Returns: pruned activity dict
  """
  keep = ['id', 'url', 'content']
  if not source.Source.is_public(activity):
    keep += ['to']
  pruned = {f: activity.get(f) for f in keep}

  obj = activity.get('object')
  if obj:
    obj = pruned['object'] = prune_activity(obj)
    for k, v in obj.items():
      if pruned.get(k) == v:
        del obj[k]

  return trim_nulls(pruned)


def original_post_discovery(account, activity):
  """Augments the standard original_post_discovery algorithm with a
  reverse lookup that supports posts without a backlink or citation.

  Args:
    account: bridgy.Source subclass
    activity: activity dict

  """
  source.Source.original_post_discovery(activity)

  obj = activity.get('object') or activity
  original = _posse_post_discovery(account, obj)
  if original:
    obj.setdefault('tags', []).append({
        'objectType': 'article',
        'url': original
      })

  return activity


def _posse_post_discovery(account, activity):
  """Supports original-post-discovery for syndicated content without a
  link to the original.

  Performs a reverse-lookup that scans the activity's author's h-feed
  for posts with rel=syndication links. As we find syndicated copies,
  save the relationship.  If we find the original pos for the activity
  in question, return the original's URL.

  See http://indiewebcamp.com/posse-post-discovery for more detail.

  Args:
    activity: the dict representing the syndicated post

  """
  from models import SyndicatedPost

  def process_author(author_url):
    # for now use whether the url is a valid webmention target
    # as a proxy for whether it's worth searching it.
    # TODO skip sites we know don't have microformats2 markup
    _, _, is_valid_target = get_webmention_target(author_url)
    if not is_valid_target:
      return

    try:
      author_resp = requests.get(author_url, timeout=HTTP_TIMEOUT)
    except BaseException:
      # TODO limit allowed failures, cache the author's h-feed url
      # or the # of times we've failed to fetch it
      logging.error("Could not fetch author url %s", author_url)
      return None

    author_parsed = Mf2Parser(url=author_url, doc=author_resp.text).to_dict()

    # look for canonical feed url (if it isn't this one) using
    # rel='feed', type='text/html'
    canonical = next(iter(author_parsed.get('rels').get('feed', [])), None)
    if canonical and canonical != author_url:
      try:
        canonical_resp = requests.get(canonical, timeout=HTTP_TIMEOUT)
        author_parsed = Mf2Parser(
          url=canonical, doc=canonical_resp.text).to_dict()
      except BaseException:
        logging.exception(
          "Could not fetch h-feed url %s. Falling back on author url.",
          canonical)

    feeditems = author_parsed['items']
    hfeed = next((item for item in feeditems
                  if 'h-feed' in item['type']), None)
    if hfeed:
      feeditems = hfeed['children']
    else:
      logging.info("No h-feed found, fallback to top-level h-entrys.")

    process_feed(feeditems)

  def process_feed(feeditems):
    """process each h-feed entry that has not been encountered before

    Args:
      feeditems: a list of mf2 dicts
    """
    permalinks = []  # an ordered set would be better
    for child in feeditems:
      if 'h-entry' in child['type']:
        for permalink in child['properties'].get('url', []):
          if not permalink in permalinks:
            permalinks.append(permalink)

    original_url = None
    for permalink in permalinks:  # TODO maybe limit to first ~30 entries?

      relationship = SyndicatedPost.query_by_original(permalink)
      # if the post hasn't already been processed
      if not relationship:
        logging.debug("parsing permalink: %s", permalink)
        process_entry(permalink)

  def process_entry(permalink):
    """Fetch and process an h-hentry, saving a new SyndicatedPost
    to the DB if successful.

    Args:
      permalink: the url of the unprocessed post
    """
    try:
      resp = requests.get(permalink, timeout=HTTP_TIMEOUT)
      parsed = Mf2Parser(url=permalink, doc=resp.text).to_dict()
    except BaseException:
      # TODO limit the number of allowed failures
      logging.error("Could not fetch permalink %s", permalink)
      return

    syndurls = set()
    relsynd = parsed.get('rels').get('syndication', [])
    logging.debug("rel-syndication links: %s", relsynd)
    syndurls.update(relsynd)

    hentry = next((item for item in parsed['items']
                   if 'h-entry' in item['type']), None)
    if hentry:
      usynd = hentry.get('properties', {}).get('syndication', [])
      logging.debug("u-syndication links: %s", usynd)
      syndurls.update(usynd)

    # remember the relationships so we don't have to re-process this permalink
    if syndurls:
      for syndurl in syndurls:
        # follow redirects to give us the canonical syndication url --
        # gives the best chance of finding a match.
        syndurl = follow_redirects(syndurl).url
        relationship = SyndicatedPost()
        relationship.original = permalink
        relationship.syndication = syndurl
        relationship.put()
    else:
      # remember that this post doesn't have syndication links
      relationship = SyndicatedPost()
      relationship.original = permalink
      relationship.put()

  # use account.domain_url instead of trusting the activity to have an
  # embedded author website
  # author_url = activity.get('author', {}).get('url')
  author_url = account.domain_url
  syndication_url = activity.get('url')

  if not author_url or not syndication_url:
    return None

  if DEBUG:
    if author_url.startswith('http://snarfed.org'):
      author_url = author_url.replace('snarfed.org', 'localhost')
    elif author_url.startswith('http://kylewm.com'):
      author_url = author_url.replace('kylewm.com', 'localhost')

  # use the canonical syndication url on both sides, so that we have
  # the best chance of finding a match. Some silos allow several
  # different permalink formats to point to the same place (e.g.,
  # facebook user id instead of user name)
  syndication_url = follow_redirects(syndication_url).url

  logging.debug("posse post discovery with author %s and syndicated %s",
                author_url, syndication_url)

  relationship = SyndicatedPost.query_by_syndication(syndication_url)
  if not relationship:
    # a silo post we haven't seen before! fetch the author's h-feed to
    # see if we can find it.
    process_author(author_url)
    relationship = SyndicatedPost.query_by_syndication(syndication_url)

  if not relationship:
    # No relationship was found. Remember that we've seen this silo
    # post to avoid reprocessing it every time
    relationship = SyndicatedPost()
    relationship.syndication = syndication_url
    relationship.put()
    return None

  return relationship.original


class Handler(webapp2.RequestHandler):
  """Includes misc request handler utilities.

  Attributes:
    messages: list of notification messages to be rendered in this page or
      wherever it redirects
  """

  def __init__(self, *args, **kwargs):
    super(Handler, self).__init__(*args, **kwargs)
    self.messages = set()

  def redirect(self, uri, **kwargs):
    """Adds self.messages to the fragment, separated by newlines.
    """
    parts = list(urlparse.urlparse(uri))
    if self.messages and not parts[5]:  # parts[5] is fragment
      parts[5] = '!' + urllib.quote('\n'.join(self.messages).encode('utf-8'))
    uri = urlparse.urlunparse(parts)
    super(Handler, self).redirect(uri, **kwargs)

  def maybe_add_or_delete_source(self, source_cls, auth_entity, state):
    """Adds or deletes a source if auth_entity is not None.

    Used in each source's oauth-dropins CallbackHandler finish() and get()
    methods, respectively.

    Args:
      source_cls: source class, e.g. Instagram
      auth_entity: ouath-dropins auth entity
      state: string, OAuth callback state parameter. For adds, this is just a
        feature ('listen' or 'publish') or empty. For deletes, it's
        [FEATURE]-[SOURCE KEY].
    """
    if state is None:
      state = ''
    if state in ('', 'listen', 'publish', 'webmention'):  # this is an add/update
      if not auth_entity:
        self.messages.add("OK, you're not signed up. Hope you reconsider!")
        self.redirect('/')
        return

      CachedFrontPage.invalidate()
      source = source_cls.create_new(self, auth_entity=auth_entity,
                                     features=[state] if state else [])
      self.redirect(source.bridgy_url(self) if source else '/')
      return source

    else:  # this is a delete
      if auth_entity:
        self.redirect('/delete/finish?auth_entity=%s&state=%s' %
                      (auth_entity.key.urlsafe(), state))
      else:
        self.messages.add("OK, you're still signed up.")
        self.redirect(source.bridgy_url(self))

  def preprocess_source(self, source):
    """Prepares a source entity for rendering in the source.html template.

    - use id as name if name isn't provided
    - convert image URLs to https if we're serving over SSL

    Args:
      source: Source entity
    """
    if not source.name:
      source.name = source.key.string_id()
    if source.picture:
      source.picture = util.update_scheme(source.picture, self)
    return source


class CachedFrontPage(ndb.Model):
  """Cached HTML for the front page, since it changes rarely.

  Stored in the datastore since datastore entities in memcache (mostly
  Responses) are requested way more often, so it would get evicted
  out of memcache easily.
  """
  ID = 'singleton'
  html = ndb.TextProperty()

  @classmethod
  def load(cls):
    cached = CachedFrontPage.get_by_id(cls.ID)
    if cached:
      logging.info('Found cached front page')
    return cached

  @classmethod
  def store(cls, html):
    logging.info('Storing new front page in cache')
    CachedFrontPage(id=cls.ID, html=html).put()

  @classmethod
  def invalidate(cls):
    logging.info('Deleting cached front page')
    CachedFrontPage(id=cls.ID).key.delete()
