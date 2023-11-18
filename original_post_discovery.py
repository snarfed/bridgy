"""Augments the standard original_post_discovery algorithm with a
reverse lookup that supports posts without a backlink or citation.

Performs a reverse-lookup that scans the activity's author's ``h-feed``
for posts with rel=syndication links. As we find syndicated copies,
save the relationship.  If we find the original post for the activity
in question, return the original's URL.

See http://indiewebcamp.com/posse-post-discovery for more detail.

This feature adds costs in terms of HTTP requests and database
lookups in the following primary cases:

* Author's domain is known to be invalid or blocklisted, there will
  be 0 requests and 0 DB lookups.
* For a syndicated post has been seen previously (regardless of
  whether discovery was successful), there will be 0 requests and 1
  DB lookup.
* The first time a syndicated post has been seen:
   * 1 to 2 HTTP requests to get and parse the ``h-feed`` plus 1 additional
     request for *each* post permalink that has not been seen before.
   * 1 DB query for the initial check plus 1 additional DB query for
     *each* post permalink.
"""
import collections
import itertools
import logging
import mf2util

from granary import as1
from granary import microformats2
from oauth_dropins.webutil.appengine_info import DEBUG
import models
from models import SyndicatedPost
import util

logger = logging.getLogger(__name__)

MAX_PERMALINK_FETCHES = 10
MAX_PERMALINK_FETCHES_BETA = 50
MAX_FEED_ENTRIES = 100
MAX_ORIGINAL_CANDIDATES = 10
MAX_MENTION_CANDIDATES = 10
# this was 30 in google.appengine.ext.ndb. haven't found it in google.cloud.ndb
# yet, or whether it's even there at all, but we only rarely hit it anyway, so
# let's just keep it as is for now.
MAX_ALLOWABLE_QUERIES = 30

MF2_HTML_MIME_TYPE= 'text/mf2+html'


def discover(source, activity, fetch_hfeed=True, include_redirect_sources=True,
             already_fetched_hfeeds=None):
  """Augments the standard original post discovery algorithm with a
  reverse lookup that supports posts without a backlink or citation.

  If ``fetch_hfeed`` is False, then we will check the db for previously found
  :class:`models.SyndicatedPost`\s but will not do posse-post-discovery to find
  new ones.

  Args:
    source (models.Source): subclass. Changes to property values (e.g.
      `domains``, ``domain_urls``, ``last_syndication_url``) are stored in
      ``source.updates``\; they should be updated transactionally later.
    activity (dict)
    fetch_hfeed (bool)
    include_redirect_sources (bool): whether to include URLs that redirect as
      well as their final destination URLs
    already_fetched_hfeeds (set of str): URLs that we have already fetched and
      run posse-post-discovery on, so we can avoid running it multiple times

  Returns:
    (set of str, set of str) tuple: (original post URLs, mention URLs)
  """
  label = activity.get('url') or activity.get('id')
  logger.debug(f'discovering original posts for: {label}')

  if not source.updates:
    source.updates = {}

  if already_fetched_hfeeds is None:
    already_fetched_hfeeds = set()

  originals, mentions = as1.original_post_discovery(
    activity, domains=source.domains,
    include_redirect_sources=include_redirect_sources,
    include_reserved_hosts=DEBUG, max_redirect_fetches=MAX_ORIGINAL_CANDIDATES,
    headers=util.request_headers(source=source))

  # only include mentions of the author themselves.
  # (mostly just for Mastodon; other silos' domains are all in the blocklist, so
  # their mention URLs get dropped later anyway.)
  # (these are originally added in Source._inject_user_urls() and in poll step 2.)
  obj = activity.get('object', {})
  other_user_mentions = set(
    t.get('url') for t in obj.get('tags', [])
    if t.get('objectType') == 'person' and t.get('url') not in source.domain_urls)
  originals -= other_user_mentions
  mentions -= other_user_mentions

  # original posts are only from the author themselves
  owner = activity.get('actor') or obj.get('author') or {}
  owner_ids = util.trim_nulls([owner.get('id'), owner.get('username')])
  source_ids = util.trim_nulls([source.key.id(), source.user_tag_id()])
  if source.USERNAME_KEY_ID:
    owner_ids = [id.lower() for id in owner_ids]
    source_ids = [id.lower() for id in source_ids]

  if owner_ids and not set(owner_ids) & set(source_ids):
    logger.info(f"Demoting original post links because user ids {source_ids} don't match author ids {owner_ids}")
    # this is someone else's post, so all links must be mentions
    mentions.update(originals)
    originals = set()

  # look for original URL of attachments (e.g. quote tweets)
  for att in obj.get('attachments', []):
    if (att.get('objectType') in ('note', 'article')
        and att.get('author', {}).get('id') == source.user_tag_id()):
      logger.debug(f"running original post discovery on attachment: {att.get('id')}")
      att_origs, _ = discover(
        source, att, include_redirect_sources=include_redirect_sources)
      logger.debug(f'original post discovery found originals for attachment, {att_origs}')
      mentions.update(att_origs)

  if len(originals) > MAX_ORIGINAL_CANDIDATES:
    logger.info(f'{len(originals)} originals, pruning down to {MAX_ORIGINAL_CANDIDATES}')
    originals = sorted(originals)[:MAX_ORIGINAL_CANDIDATES]
  if len(mentions) > MAX_MENTION_CANDIDATES:
    logger.info(f'{len(mentions)} mentions, pruning down to {MAX_MENTION_CANDIDATES}')
    mentions = sorted(mentions)[:MAX_MENTION_CANDIDATES]

  def resolve(urls):
    resolved = set()
    for url in urls:
      final, domain, send = util.get_webmention_target(url)
      if send and domain != source.gr_source.DOMAIN:
        resolved.add(final)
        if include_redirect_sources:
          resolved.add(url)
    return resolved

  originals = resolve(originals)
  mentions = resolve(mentions)

  if not source.get_author_urls():
    logger.debug('no author url(s), cannot find h-feed')
    return ((originals, mentions) if not source.BACKFEED_REQUIRES_SYNDICATION_LINK
            else (set(), set()))

  # TODO possible optimization: if we've discovered a backlink to a post on the
  # author's domain (i.e., it included a link or citation), then skip the rest
  # of this.
  syndicated = []
  syndication_url = obj.get('url') or activity.get('url')
  if syndication_url:
    # use the canonical syndication url on both sides, so that we have
    # the best chance of finding a match. Some silos allow several
    # different permalink formats to point to the same place.
    syndication_url = source.canonicalize_url(syndication_url)
    if syndication_url:
      syndicated = _posse_post_discovery(source, activity, syndication_url,
                                         fetch_hfeed, already_fetched_hfeeds)
      originals.update(syndicated)
    originals = set(util.dedupe_urls(originals))

  if not syndication_url:
    logger.debug(f'no {source.SHORT_NAME} syndication url, cannot process h-entries')

  return ((originals, mentions) if not source.BACKFEED_REQUIRES_SYNDICATION_LINK
          else (set(syndicated), set()))


def refetch(source):
  """Refetch the author's URLs and look for new or updated syndication
  links that might not have been there the first time we looked.

  Args:
    source (models.Source): Changes to property values (e.g. ``domains``,
      ``domain_urls``, ``last_syndication_url``) are stored in source.updates;
      they should be updated transactionally later.

  Returns:
    dict: mapping syndicated_url to a list of new :class:`models.SyndicatedPost`\s
  """
  logger.debug(f'attempting to refetch h-feed for {source.label()}')

  if not source.updates:
    source.updates = {}

  results = {}
  for url in _get_author_urls(source):
    results.update(_process_author(source, url, refetch=True))

  return results


def targets_for_response(resp, originals, mentions):
  """Returns the URLs that we should send webmentions to for a given response.

  ...specifically, all responses except posts get sent to original post URLs,
  but only posts and comments get sent to mentioned URLs.

  Args:
    resp (dict): ActivityStreams response object
    originals, mentions (sequence of str) URLs

  Returns:
    set of str: URLs
  """
  type = models.Response.get_type(resp)
  targets = set()
  if type != 'post':
    targets |= originals
  if type in ('post', 'comment'):
    targets |= mentions
  return targets


def _posse_post_discovery(source, activity, syndication_url, fetch_hfeed,
                          already_fetched_hfeeds):
  """Performs the actual meat of the posse-post-discover.

  Args:
    source (models.Source)
    activity (dict)
    syndication_url (str): url of the syndicated copy for which we are
      trying to find an original
    fetch_hfeed (bool): whether or not to fetch and parse the
      author's feed if we don't have a previously stored
      relationship
    already_fetched_hfeeds (set of str): URLs we've already fetched in a
      previous iteration

  Return:
    list of str: original post urls, possibly empty
  """
  logger.info(f'starting posse post discovery with syndicated {syndication_url}')

  relationships = SyndicatedPost.query(
    SyndicatedPost.syndication == syndication_url,
    ancestor=source.key).fetch()

  if source.IGNORE_SYNDICATION_LINK_FRAGMENTS:
    relationships += SyndicatedPost.query(
      # prefix search to find any instances of this synd link with a fragment
      SyndicatedPost.syndication > f'{syndication_url}#',
      SyndicatedPost.syndication < f'{syndication_url}#\ufffd',
      ancestor=source.key).fetch()

  if not relationships and fetch_hfeed:
    # a syndicated post we haven't seen before! fetch the author's URLs to see
    # if we can find it.
    #
    # TODO: Consider using the actor's url, with get_author_urls() as the
    # fallback in the future to support content from non-Bridgy users.
    results = {}
    for url in _get_author_urls(source):
      if url not in already_fetched_hfeeds:
        results.update(_process_author(source, url))
        already_fetched_hfeeds.add(url)
      else:
        logger.debug(f'skipping {url}, already fetched this round')

    relationships = results.get(syndication_url, [])

  if not relationships:
    # No relationships were found. Remember that we've seen this
    # syndicated post to avoid reprocessing it every time
    logger.debug(f'posse post discovery found no relationship for {syndication_url}')
    if fetch_hfeed:
      SyndicatedPost.insert_syndication_blank(source, syndication_url)

  originals = [r.original for r in relationships if r.original]
  if originals:
    logger.debug(f'posse post discovery found relationship(s) {syndication_url} -> {originals}')
  return originals


def _process_author(source, author_url, refetch=False, store_blanks=True):
  """Fetch the author's domain URL, and look for syndicated posts.

  Args:
    source (models.Source)
    author_url (str): the author's homepage URL
    refetch (bool): whether to refetch and process entries we've seen before
    store_blanks (bool): whether we should store blank
      :class:`models.SyndicatedPost`\s when we don't find a relationship

  Return:
    dict: maps syndicated_url to a list of new :class:`models.SyndicatedPost`\s
  """
  # for now use whether the url is a valid webmention target
  # as a proxy for whether it's worth searching it.
  author_url, _, ok = util.get_webmention_target(author_url)
  if not ok:
    return {}

  logger.debug(f'fetching author url {author_url}')
  try:
    author_mf2 = util.fetch_mf2(author_url)
  except AssertionError:
    raise  # for unit tests
  except BaseException:
    # TODO limit allowed failures, cache the author's h-feed url
    # or the # of times we've failed to fetch it
    logger.info(f'Could not fetch author url {author_url}', exc_info=True)
    return {}

  if not author_mf2:
    logger.debug('nothing found')
    return {}

  feeditems = _find_feed_items(author_mf2)

  # try rel=feeds and rel=alternates
  feed_urls = set()
  candidates = (author_mf2['rels'].get('feed', []) +
                [a.get('url') for a in author_mf2.get('alternates', [])
                 if a.get('type') == MF2_HTML_MIME_TYPE])
  for feed_url in candidates:
    # check that it's html, not too big, etc
    feed_url, _, feed_ok = util.get_webmention_target(feed_url)
    if feed_url == author_url:
      logger.debug('author url is the feed url, ignoring')
    elif not feed_ok:
      logger.debug("skipping feed since it's not HTML or otherwise bad")
    else:
      feed_urls.add(feed_url)

  for feed_url in feed_urls:
    try:
      logger.debug(f"fetching author's rel-feed {feed_url}")
      feed_mf2 = util.fetch_mf2(feed_url)
      if not feed_mf2:
        logger.debug('nothing found')
        continue
      feeditems = _merge_hfeeds(feeditems, _find_feed_items(feed_mf2))
      domain = util.domain_from_link(feed_url)
      if source.updates is not None and domain not in source.domains:
        domains = source.updates.setdefault('domains', source.domains)
        if domain not in domains:
          logger.info(f'rel-feed found new domain {domain}! adding to source')
          domains.append(domain)

    except AssertionError:
      raise  # reraise assertions for unit tests
    except BaseException:
      logger.info(f'Could not fetch h-feed url {feed_url}.', exc_info=True)

  # sort by dt-updated/dt-published
  def updated_or_published(item):
    props = microformats2.first_props(item.get('properties'))
    return props.get('updated') or props.get('published') or ''

  feeditems.sort(key=updated_or_published, reverse=True)

  permalink_to_entry = collections.OrderedDict()
  for child in feeditems:
    if 'h-entry' in child['type']:
      permalinks = child['properties'].get('url', [])
      if not permalinks:
        logger.debug('ignoring h-entry with no u-url!')
      for permalink in permalinks:
        if isinstance(permalink, str):
          permalink_to_entry[permalink] = child
        else:
          logger.warning(f'unexpected non-string "url" property: {permalink}')

    max = (MAX_PERMALINK_FETCHES_BETA if source.is_beta_user()
           else MAX_PERMALINK_FETCHES)
    if len(permalink_to_entry) >= max:
      logger.info(f'Hit cap of {max} permalinks. Stopping.')
      break

  # query all preexisting permalinks at once, instead of once per link
  permalinks_list = list(permalink_to_entry.keys())
  # fetch the maximum allowed entries (currently 30) at a time
  preexisting_list = itertools.chain.from_iterable(
    SyndicatedPost.query(
      SyndicatedPost.original.IN(permalinks_list[i:i + MAX_ALLOWABLE_QUERIES]),
      ancestor=source.key)
    for i in range(0, len(permalinks_list), MAX_ALLOWABLE_QUERIES))
  preexisting = {}
  for r in preexisting_list:
    preexisting.setdefault(r.original, []).append(r)

  results = {}
  for permalink, entry in permalink_to_entry.items():
    logger.debug(f'processing permalink: {permalink}')
    new_results = process_entry(
      source, permalink, entry, refetch, preexisting.get(permalink, []),
      store_blanks=store_blanks)
    for key, value in new_results.items():
      results.setdefault(key, []).extend(value)

  if source.updates is not None and results:
    # keep track of the last time we've seen rel=syndication urls for
    # this author. this helps us decide whether to refetch periodically
    # and look for updates.
    # Source will be saved at the end of each round of polling
    source.updates['last_syndication_url'] = util.now()

  return results


def _merge_hfeeds(feed1, feed2):
  """Merge items from two ``h-feeds`` into a composite feed.

  Skips items in ``feed2`` that are already represented in ``feed1``\, based on
  the ``url`` property.

  Args:
    feed1 (list of dict)
    feed2 (list of dict)

  Returns:
    list of dict:
  """
  seen = set()
  for item in feed1:
    for url in item.get('properties', {}).get('url', []):
      if isinstance(url, str):
        seen.add(url)

  return feed1 + [item for item in feed2 if all(
    (url not in seen) for url in item.get('properties', {}).get('url', []) if isinstance(url, str))]


def _find_feed_items(mf2):
  """Extract feed items from given microformats2 data.

  If the top-level ``h-*`` item is an h-feed, return its children. Otherwise,
  returns the top-level items.

  Args:
    mf2 (dict): parsed mf2 data

  Returns:
    list of dict: each one representing an mf2 ``h-*`` item
  """
  feeditems = mf2['items']
  hfeeds = mf2util.find_all_entries(mf2, ('h-feed',))
  if hfeeds:
    feeditems = list(itertools.chain.from_iterable(
      hfeed.get('children', []) for hfeed in hfeeds))
  else:
    logger.debug('No h-feed found, fallback to top-level h-entrys.')

  if len(feeditems) > MAX_FEED_ENTRIES:
    logger.info(f'Feed has {len(feeditems)} entries! only processing the first {MAX_FEED_ENTRIES}.')
    feeditems = feeditems[:MAX_FEED_ENTRIES]

  return feeditems


def process_entry(source, permalink, feed_entry, refetch, preexisting,
                  store_blanks=True):
  """Fetch and process an h-entry and save a new :class:`models.SyndicatedPost`.

  Args:
    source (models.Source)
    permalink (str): url of the unprocessed post
    feed_entry (dict): the ``h-feed`` version of the ``h-entry``\, often contains
      a partial version of the ``h-entry`` at the permalink
    refetch (bool): whether to refetch and process entries we've seen before
    preexisting (list): of previously discovered :class:`models.SyndicatedPost`\s
      for this permalink
    store_blanks (bool): whether we should store blank
      :class:`models.SyndicatedPost`\s when we don't find a relationship

  Returns:
    dict: maps syndicated url to a list of new :class:`models.SyndicatedPost`\s
  """
  # if the post has already been processed, do not add to the results
  # since this method only returns *newly* discovered relationships.
  if preexisting:
    # if we're refetching and this one is blank, do not return.
    # if there is a blank entry, it should be the one and only entry,
    # but go ahead and check 'all' of them to be safe.
    if not refetch:
      return {}
    synds = [s.syndication for s in preexisting if s.syndication]
    if synds:
      logger.debug(f'previously found relationship(s) for original {permalink}: {synds}')

  # first try with the h-entry from the h-feed. if we find the syndication url
  # we're looking for, we don't have to fetch the permalink
  permalink, _, type_ok = util.get_webmention_target(permalink)
  usynd = feed_entry.get('properties', {}).get('syndication', [])
  usynd_urls = {url for url in usynd if isinstance(url, str)}
  if usynd_urls:
    logger.debug(f'u-syndication links on the h-feed h-entry: {usynd_urls}')
  results = _process_syndication_urls(source, permalink, usynd_urls, preexisting)
  success = True

  if results:
    source.updates['last_feed_syndication_url'] = util.now()
  elif not source.last_feed_syndication_url or not feed_entry:
    # fetch the full permalink page if we think it might have more details
    mf2 = None
    try:
      if type_ok:
        logger.debug(f'fetching post permalink {permalink}')
        mf2 = util.fetch_mf2(permalink)
    except AssertionError:
      raise  # for unit tests
    except BaseException:
      # TODO limit the number of allowed failures
      logger.info(f'Could not fetch permalink {permalink}', exc_info=True)
      success = False

    if mf2:
      syndication_urls = set()
      relsynd = mf2['rels'].get('syndication', [])
      if relsynd:
        logger.debug(f'rel-syndication links: {relsynd}')
      syndication_urls.update(url for url in relsynd
                              if isinstance(url, str))
      # there should only be one h-entry on a permalink page, but
      # we'll check all of them just in case.
      for hentry in (item for item in mf2['items']
                     if 'h-entry' in item['type']):
        usynd = hentry.get('properties', {}).get('syndication', [])
        if usynd:
          logger.debug(f'u-syndication links: {usynd}')
        syndication_urls.update(url for url in usynd
                                if isinstance(url, str))
      results = _process_syndication_urls(
        source, permalink, syndication_urls, preexisting)

  # detect and delete SyndicatedPosts that were removed from the site
  if success:
    result_syndposts = list(itertools.chain(*results.values()))
    for syndpost in preexisting:
      if syndpost.syndication and syndpost not in result_syndposts:
        logger.info(f'deleting relationship that disappeared: {syndpost}')
        syndpost.key.delete()
        preexisting.remove(syndpost)

  if not results:
    logger.debug(f'no syndication links from {permalink} to current source {source.label()}.')
    results = {}
    if store_blanks and not preexisting:
      # remember that this post doesn't have syndication links for this
      # particular source
      logger.debug(f'saving empty relationship so that {permalink} will not be searched again')
      SyndicatedPost.insert_original_blank(source, permalink)

  # only return results that are not in the preexisting list
  new_results = {}
  for syndurl, syndposts_for_url in results.items():
    for syndpost in syndposts_for_url:
      if syndpost not in preexisting:
        new_results.setdefault(syndurl, []).append(syndpost)

  if new_results:
    logger.debug(f'discovered relationships {new_results}')
  return new_results


def _process_syndication_urls(source, permalink, syndication_urls,
                              preexisting):
  """Process a list of syndication URLs looking for one that matches the
  current source. If one is found, stores a new :class:`models.SyndicatedPost`
  in the db.

  Args:
    source (models.Source)
    permalink (str): the current ``h-entry`` permalink
    syndication_urls (sequence of str): the unfitered list of syndication urls
    preexisting: list of models.SyndicatedPost: previously discovered

  Returns:
    dict: maps str syndication url to list of :class:`models.SyndicatedPost`\s
  """
  results = {}
  # save the results (or lack thereof) to the db, and put them in a
  # map for immediate use
  for url in syndication_urls:
    # source-specific logic to standardize the URL
    url = source.canonicalize_url(url)
    if not url:
      continue

    # TODO: save future lookups by saving results for other sources too (note:
    # query the appropriate source subclass by author.domains, rather than
    # author.domain_urls)
    #
    # we may have already seen this relationship, save a DB lookup by
    # finding it in the preexisting list
    relationship = next((sp for sp in preexisting
                         if sp.syndication == url
                         and sp.original == permalink), None)
    if not relationship:
      logger.debug(f'saving discovered relationship {url} -> {permalink}')
      relationship = SyndicatedPost.insert(source, syndication=url, original=permalink)
    results.setdefault(url, []).append(relationship)

  return results


def _get_author_urls(source):
  max = models.MAX_AUTHOR_URLS
  urls = source.get_author_urls()
  if len(urls) > max:
    logger.warning(f'user has over {max} URLs! only running PPD on {urls[:max]}. skipping {urls[max:]}.')
    urls = urls[:max]

  return urls
