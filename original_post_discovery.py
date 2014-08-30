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

import datetime
import itertools
import logging
import mf2py
import requests
import urlparse
import util

from activitystreams import source as as_source
from appengine_config import HTTP_TIMEOUT
from google.appengine.api.datastore import MAX_ALLOWABLE_QUERIES
from bs4 import BeautifulSoup
from models import SyndicatedPost

# alias allows unit tests to mock the function
now_fn = datetime.datetime.now


def discover(source, activity, fetch_hfeed=True):
  """Augments the standard original_post_discovery algorithm with a
  reverse lookup that supports posts without a backlink or citation.

  If fetch_hfeed is False, then we will check the db for previously
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

  # Use source.domain_urls for now; it seems more reliable than the
  # activity.actor.url (which depends on getting the right data back from
  # various APIs). Consider using the actor's url, with domain_urls as the
  # fallback in the future to support content from non-Bridgy users.
  #
  # author_url = activity.get('actor', {}).get('url')
  obj = activity.get('object') or activity
  author_url = source.get_author_url()
  syndication_url = obj.get('url')

  if not author_url:
    logging.debug('no author url, cannot find h-feed %s', author_url)
    return activity

  if not syndication_url:
    logging.debug('no syndication url, cannot process h-entries %s',
                  syndication_url)
    return activity

  # use the canonical syndication url on both sides, so that we have
  # the best chance of finding a match. Some silos allow several
  # different permalink formats to point to the same place (e.g.,
  # facebook user id instead of user name)
  syndication_url = source.canonicalize_syndication_url(
    util.follow_redirects(syndication_url).url)

  return _posse_post_discovery(source, activity,
                               author_url, syndication_url,
                               fetch_hfeed)


def refetch(source):
  """Refetch the author's url and look for new or updated syndication
  links that might not have been there the first time we looked.

  Args:
    source: a models.Source subclass

  Return:
    a dict of syndicated_url to a list of new models.SyndicatedPosts
  """

  logging.debug('attempting to refetch h-feed for %s', source.label())
  author_url = source.get_author_url()

  if not author_url:
    logging.debug('no author url, cannot find h-feed %s', author_url)
    return {}

  return _process_author(source, author_url, refetch_blanks=True)


def _posse_post_discovery(source, activity, author_url, syndication_url,
                          fetch_hfeed):
  """Performs the actual meat of the posse-post-discover. It was split
  out from discover() so that it can be done inside of a transaction.

  Args:
    source: models.Source subclass
    activity: activity dict
    author_url: author's url configured in their silo profile
    syndication_url: url of the syndicated copy for which we are
                     trying to find an original
    fetch_hfeed: boolean, whether or not to fetch and parse the
                 author's feed if we don't have a previously stored
                 relationship.

  Return:
    the activity, updated with original post urls if any are found
  """
  logging.info(
      'starting posse post discovery with author %s and syndicated %s',
      author_url, syndication_url)

  relationships = SyndicatedPost.query(
    SyndicatedPost.syndication == syndication_url,
    ancestor=source.key).fetch()
  if not relationships and fetch_hfeed:
    # a syndicated post we haven't seen before! fetch the author's
    # h-feed to see if we can find it.
    results = _process_author(source, author_url)
    relationships = results.get(syndication_url)

  if not relationships:
    # No relationships were found. Remember that we've seen this
    # syndicated post to avoid reprocessing it every time
    logging.debug('posse post discovery found no relationship for %s',
                  syndication_url)
    SyndicatedPost.insert_syndication_blank(source, syndication_url)
    return activity

  logging.debug('posse post discovery found relationship(s) %s -> %s',
                syndication_url,
                '; '.join(str(r.original) for r in relationships))

  obj = activity.get('object') or activity
  obj.setdefault('upstreamDuplicates', []).extend(
    r.original for r in relationships if r.original)

  return activity


def _process_author(source, author_url, refetch_blanks=False):
  """Fetch the author's domain URL, and look for syndicated posts.

  Args:
    source: a subclass of models.Source
    author_url: the author's homepage URL
    refetch_blanks: boolean, if true, refetch SyndicatedPosts that have
      previously been marked as not having a rel=syndication link

  Return:
    a dict of syndicated_url to a list of new models.SyndicatedPost
  """
  # for now use whether the url is a valid webmention target
  # as a proxy for whether it's worth searching it.
  # TODO skip sites we know don't have microformats2 markup
  author_url, _, ok = util.get_webmention_target(author_url)
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
    logging.warning('Could not fetch author url %s', author_url, exc_info=True)
    return {}

  author_dom = BeautifulSoup(author_resp.text)
  author_parser = mf2py.Parser(url=author_url, doc=author_dom)
  author_parsed = author_parser.to_dict()

  # look for canonical feed url (if it isn't this one) using
  # rel='feed', type='text/html'
  for rel_feed_node in (author_dom.find_all('link', rel='feed')
                        + author_dom.find_all('a', rel='feed')):
    feed_url = rel_feed_node.get('href')
    if not feed_url:
      continue

    feed_url = urlparse.urljoin(author_url, feed_url)
    feed_type = rel_feed_node.get('type')
    if not feed_type:
      # type is not specified, use this to confirm that it's text/html
      feed_url, _, feed_type_ok = util.get_webmention_target(feed_url)
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
      author_parsed = mf2py.Parser(
        url=feed_url, doc=feed_resp.text).to_dict()
      break
    except AssertionError:
      raise  # reraise assertions for unit tests
    except BaseException:
      logging.warning('Could not fetch h-feed url %s.', feed_url,
                      exc_info=True)

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
        if isinstance(permalink, basestring):
          permalinks.add(permalink)
        else:
          logging.warn('unexpected non-string "url" property: %s', permalink)

  # query all preexisting permalinks at once, instead of once per link
  permalinks_list = list(permalinks)
  # fetch the maximum allowed entries (currently 30) at a time
  preexisting_list = itertools.chain.from_iterable(
    SyndicatedPost.query(
      SyndicatedPost.original.IN(permalinks_list[i:i + MAX_ALLOWABLE_QUERIES]),
      ancestor=source.key)
    for i in xrange(0, len(permalinks_list), MAX_ALLOWABLE_QUERIES))
  preexisting = {}
  for r in preexisting_list:
    preexisting.setdefault(r.original, []).append(r)

  results = {}
  for permalink in permalinks:
    logging.debug('processing permalink: %s', permalink)
    new_results = _process_entry(source, permalink, refetch_blanks,
                                 preexisting.get(permalink, []))
    for key, value in new_results.iteritems():
      results.setdefault(key, []).extend(value)

  if results:
    # keep track of the last time we've seen rel=syndication urls for
    # this author. this helps us decide whether to refetch periodically
    # and look for updates.
    # Source will be saved at the end of each round of polling
    now = now_fn()
    logging.debug('updating source.last_syndication_url %s', now)
    source.last_syndication_url = now

  return results


def _process_entry(source, permalink, refetch_blanks, preexisting):
  """Fetch and process an h-entry, saving a new SyndicatedPost to the
  DB if successful.

  Args:
    permalink: url of the unprocessed post
    syndication_url: url of the syndicated content
    refetch_blanks: boolean whether we should ignore blank preexisting
      SyndicatedPosts
    preexisting: a list of previously discovered models.SyndicatedPosts
      for this permalink

  Returns:
    a dict from syndicated url to a list of new models.SyndicatedPosts
  """
  results = {}

  # if the post has already been processed, do not add to the results
  # since this method only returns *newly* discovered relationships.
  if preexisting:
    # if we're refetching blanks and this one is blank, do not return.
    # if there is a blank entry, it should be the one and only entry,
    # but go ahead and check 'all' of them to be safe.
    if refetch_blanks and all(not p.syndication for p in preexisting):
      logging.debug('ignoring blank relationship for original %s', permalink)
    else:
      return results

  syndication_urls = set()
  parsed = None
  try:
    logging.debug('fetching post permalink %s', permalink)
    permalink, _, type_ok = util.get_webmention_target(permalink)
    if type_ok:
      resp = requests.get(permalink, timeout=HTTP_TIMEOUT)
      resp.raise_for_status()
      parsed = mf2py.Parser(url=permalink, doc=resp.text).to_dict()
  except BaseException:
    # TODO limit the number of allowed failures
    logging.warning('Could not fetch permalink %s', permalink, exc_info=True)

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
    # source-specific logic to standardize the URL. (e.g., replace facebook
    # username with numeric id)
    syndication_url = source.canonicalize_syndication_url(syndication_url)
    # check that the syndicated url belongs to this source TODO save future
    # lookups by saving results for other sources too (note: query the
    # appropriate source subclass by author.domains, rather than
    # author.domain_urls)
    parsed = urlparse.urlparse(syndication_url)
    if util.domain_from_link(parsed.netloc) == source.AS_CLASS.DOMAIN:
      logging.debug('saving discovered relationship %s -> %s',
                    syndication_url, permalink)
      relationship = SyndicatedPost.insert(
        source, syndication=syndication_url, original=permalink)
      results.setdefault(syndication_url, []).append(relationship)

  if not results:
    logging.debug('no syndication links from %s to current source %s.',
                  permalink, source.label())
    if not preexisting:
      # remember that this post doesn't have syndication links for this
      # particular source
      logging.debug('saving empty relationship so that it %s will not be '
                    'searched again', permalink)
      SyndicatedPost.insert_original_blank(source, permalink)

  logging.debug('discovered relationships %s', results)
  return results
