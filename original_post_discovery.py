"""Augments the standard original_post_discovery algorithm with a
reverse lookup that supports posts without a backlink or citation.

Performs a reverse-lookup that scans the activity's author's h-feed
for posts with rel=syndication links. As we find syndicated copies,
save the relationship.  If we find the original post for the activity
in question, return the original's URL.

See http://indiewebcamp.com/posse-post-discovery for more detail.

This feature adds costs in terms of HTTP requests and database
lookups in the following primary cases:

- Author's domain is known to be invalid or blacklisted, there will
  be 0 requests and 0 DB lookups.

- For a syndicated post has been seen previously (regardless of
  whether discovery was successful), there will be 0 requests and 1
  DB lookup.

- The first time a syndicated post has been seen:
  - 1 to 2 HTTP requests to get and parse the h-feed plus 1 additional
    request for *each* post permalink that has not been seen before.
  - 1 DB query for the initial check plus 1 additional DB query for
    *each* post permalink.
"""

import logging
import requests
import urlparse
import util

from activitystreams import source as as_source
from appengine_config import HTTP_TIMEOUT, DEBUG
from google.appengine.ext import ndb
from mf2py.parser import Parser as Mf2Parser
from models import SyndicatedPost


def discover(source, activity, fetch_hfeed=True):
  """Augments the standard original_post_discovery algorithm with a
  reverse lookup that supports posts without a backlink or citation.

  If fetch_feed is False, then we will check the db for previously
  found SyndicatedPosts but will not do posse-post-discovery to find
  new ones.

  Args:
    source: models.Source subclass
    activity: activity dict
    fetch_hfeed: boolean

  Return:
    the activity, updated with original post urls if any are found
  """
  as_source.Source.original_post_discovery(activity)

  # TODO possible optimization: if we've discovered a backlink to a
  # post on the author's domain (i.e., it included a link or
  # citation), then skip the rest of this.

  # Use source.domain_url for now; it seems more reliable than the
  # activity.actor.url (which depends on getting the right data back
  # from various APIs). Consider using the actor's url, with
  # domain_url as the fallback in the future to support content from
  # non-Bridgy users.
  # author_url = activity.get('actor', {}).get('url')
  obj = activity.get('object') or activity
  author_url = source.domain_url
  syndication_url = obj.get('url')

  if not author_url:
    logging.debug('no author url, cannot find h-feed %s', author_url)
    return activity

  if not syndication_url:
    logging.debug('no syndication url, cannot process h-entries %s',
                  syndication_url)
    return activity

  if DEBUG:
    if author_url.startswith('https://snarfed.org'):
      author_url = author_url.replace('snarfed.org', 'localhost')
    elif author_url.startswith('http://kylewm.com'):
      author_url = author_url.replace('kylewm.com', 'localhost')

  # use the canonical syndication url on both sides, so that we have
  # the best chance of finding a match. Some silos allow several
  # different permalink formats to point to the same place (e.g.,
  # facebook user id instead of user name)
  syndication_url = util.follow_redirects(syndication_url).url
  return _posse_post_discovery(source, activity,
                               author_url, syndication_url)


# TODO narrow the scope of this transaction. With a large h-feed,
# we could easily go over the 60s tx limit.
@ndb.transactional
def _posse_post_discovery(source, activity, author_url, syndication_url):
  """Performs the actual meat of the posse-post-discover. It was split
  out from discover() so that it can be done inside of a transaction.

  Args:
    source: models.Source subclass
    activity: activity dict
    author_url: author's url configured in their silo profile
    syndication_url: url of the syndicated copy for which we are
                     trying to find an original

  Return:
    the activity, updated with original post urls if any are found
  """
  logging.debug('starting posse post discovery with author %s and syndicated %s',
                author_url, syndication_url)

  relationship = SyndicatedPost.query_by_syndication(source, syndication_url)
  if not relationship:
    # a syndicated post we haven't seen before! fetch the author's
    # h-feed to see if we can find it.
    results = _process_author(source, author_url)
    relationship = results.get(syndication_url, None)

  if not relationship:
    # No relationship was found. Remember that we've seen this
    # syndicated post to avoid reprocessing it every time
    logging.debug('posse post discovery found no relationship for %s',
                  syndication_url)
    SyndicatedPost(parent=source.key, original=None,
                   syndication=syndication_url).put()
    return activity

  logging.debug('posse post discovery found relationship %s -> %s',
                syndication_url, relationship.original)

  if relationship.original:
    obj = activity.get('object') or activity
    # if an original was discovered by regular original-post-discovery,
    # then this will create a duplicate. this is ok because duplicates are
    # cleaned up later in tasks.get_webmention_targets
    obj.setdefault('tags', []).append({
      'objectType': 'article',
      'url': relationship.original,
    })

  return activity


def _process_author(source, author_url):
  """Fetch the author's domain URL, and look for syndicated posts.

  Args:
    source: a subclass of models.Source
    author_url: the author's homepage URL

  Return:
    a dict of syndicated_url to models.SyndicatedPost
  """
  # for now use whether the url is a valid webmention target
  # as a proxy for whether it's worth searching it.
  # TODO skip sites we know don't have microformats2 markup
  _, _, ok = util.get_webmention_target(author_url)
  if not ok:
    return {}

  try:
    logging.debug('fetching author domain %s', author_url)
    author_resp = requests.get(author_url, timeout=HTTP_TIMEOUT)
    # TODO for error codes that indicate a temporary error, should we make
    # a certain number of retries before giving up forever?
    author_resp.raise_for_status()
  except AssertionError:
    raise  # for unit tests
  except BaseException:
    # TODO limit allowed failures, cache the author's h-feed url
    # or the # of times we've failed to fetch it
    logging.exception('Could not fetch author url %s', author_url)
    return {}

  author_parser = Mf2Parser(url=author_url, doc=author_resp.text)
  author_parsed = author_parser.to_dict()

  # look for canonical feed url (if it isn't this one) using
  # rel='feed', type='text/html'
  # TODO clean up this private reference when mf2py is updated
  for rel_feed_node in author_parser.__doc__.find_all('link', rel='feed'):
    feed_url = rel_feed_node.get('href')
    if not feed_url:
      continue

    feed_url = urlparse.urljoin(author_url, feed_url)
    feed_type = rel_feed_node.get('type')
    if not feed_type:
      feed_resolved = util.follow_redirects(feed_url)
      if feed_resolved.status_code != 200:
        logging.debug(
          'follow_redirects for %s returned unxpected status code %d',
          feed_url, feed_resolved.status_code)
        continue
      feed_type = feed_resolved.headers.get('content-type', '')
      feed_type_ok = feed_type.startswith('text/html')
      feed_url = feed_resolved.url
      logging.debug('follow_redirects for %s determined content type %s',
                    feed_url, feed_type)
    else:
      feed_type_ok = feed_type == 'text/html'

    if feed_url == author_url:
      logging.debug('author url is the feed url, proceeding')
      break
    elif not feed_type_ok:
      logging.debug('skipping feed of type %s', feed_type)
      continue

    try:
      logging.debug("fetching author's h-feed %s", feed_url)
      feed_resp = requests.get(feed_url, timeout=HTTP_TIMEOUT)
      feed_resp.raise_for_status()
      logging.debug("author's h-feed fetched successfully %s", feed_url)
      author_parsed = Mf2Parser(
        url=feed_url, doc=feed_resp.text).to_dict()
      break
    except AssertionError:
      raise  # reraise assertions for unit tests
    except BaseException:
      logging.exception('Could not fetch h-feed url %s.', feed_url)

  feeditems = author_parsed['items']
  hfeed = next((item for item in feeditems
                if 'h-feed' in item['type']), None)
  if hfeed:
    feeditems = hfeed.get('children', [])
  else:
    logging.info('No h-feed found, fallback to top-level h-entrys.')

  permalinks = set()
  for child in feeditems:
    if 'h-entry' in child['type']:
      # TODO if this h-entry in the h-feed has u-syndication links, we
      # can just use it without fetching its permalink page
      # TODO maybe limit to first ~30 entries? (do that here rather than,
      # below because we want the *first* n entries)
      for permalink in child['properties'].get('url', []):
        permalinks.add(permalink)

  results = {}
  for permalink in permalinks:
    # TODO replace this with one query for the Source as a
    # whole. querying each permalink individually is expensive.
    relationship = SyndicatedPost.query_by_original(source, permalink)
    # if the post hasn't already been processed
    if not relationship:
      logging.debug('processing permalink: %s', permalink)
      results.update(_process_entry(source, permalink))

  return results


def _process_entry(source, permalink):
  """Fetch and process an h-hentry, saving a new SyndicatedPost to the
  DB if successful.

  Args:
    permalink: url of the unprocessed post
    syndication_url: url of the syndicated content

  Return:
    a map from syndicated url to new models.SyndicatedPosts
  """
  syndication_urls = set()
  results = {}
  parsed = None
  try:
    logging.debug('fetching post permalink %s', permalink)
    resp = requests.get(permalink, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    parsed = Mf2Parser(url=permalink, doc=resp.text).to_dict()
  except BaseException:
    # TODO limit the number of allowed failures
    logging.exception('Could not fetch permalink %s', permalink)

  if parsed:
    relsynd = parsed.get('rels').get('syndication', [])
    logging.debug('rel-syndication links: %s', relsynd)
    syndication_urls.update(relsynd)

    # there should only be one h-entry on a permalink page, but
    # we'll check all of them just in case.
    for hentry in (item for item in parsed['items']
                   if 'h-entry' in item['type']):
      usynd = hentry.get('properties', {}).get('syndication', [])
      logging.debug('u-syndication links: %s', usynd)
      syndication_urls.update(usynd)

  # save the results (or lack thereof) to the db, and put them in a
  # map for immediate use
  for syndication_url in syndication_urls:
    # follow redirects to give us the canonical syndication url --
    # gives the best chance of finding a match.
    syndication_url = util.follow_redirects(syndication_url).url
    # check that the syndicated url belongs to this source
    # TODO save future lookups by saving results for other sources
    # too (note: query the appropriate source subclass by
    # author.domain, rather than author.domain_url)
    parsed = urlparse.urlparse(syndication_url)
    if parsed.netloc == source.AS_CLASS.DOMAIN:
      logging.debug('saving discovered relationship %s -> %s',
                    syndication_url, permalink)
      relationship = SyndicatedPost(parent=source.key, original=permalink,
                                    syndication=syndication_url)
      relationship.put()
      results[syndication_url] = relationship

  if not results:
    logging.debug('no syndication links from %s to current source %s. '
                  'saving empty relationship so that it will not be '
                  'searched again', permalink, source)
    # remember that this post doesn't have syndication links for this
    # particular source
    SyndicatedPost(parent=source.key, original=permalink,
                   syndication=None).put()

  return results
