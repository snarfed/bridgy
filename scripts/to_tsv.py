#!/usr/local/bin/python
"""Generate growth data since launch using datastore admin backup files.

Outputs TSV files for each entity kind and a growth.tsv file with daily counts
for all kinds and other features. It's ugly, inadequately commented, poorly
tested, etc. Don't use it for anything remotely important!

I used this to generate the graphs in
https://snarfed.org/2014-11-06_happy-1000th-bridgy
(by importing the output into
https://docs.google.com/spreadsheets/d/1VhGiZ9Z9PEl7f9ciiVZZgupNcUTsRVltQ8_CqFETpfU/edit?usp=docslist_api )

Datastore admin backups are LevelDB log files. This code is based on:
http://gbayer.com/big-data/app-engine-datastore-how-to-efficiently-export-your-data/

More details:
https://cloud.google.com/appengine/docs/adminconsole/datastoreadmin#Enable_datastore_admin
http://leveldb.googlecode.com/svn/trunk/doc/log_format.txt
http://orcaman.blogspot.com/2014/09/exporting-gae-datastore-data-to-mongodb.html (search for 3.)
"""

import collections
import csv
import datetime
import glob
import itertools
import sys
import urlparse

sys.path.append('/usr/local/google_appengine')

from google.appengine.api.files import records
from google.appengine.datastore import entity_pb
from google.appengine.api import datastore

SOURCE_KINDS = ('Blogger', 'FacebookPage', 'GooglePlusPage', 'Instagram',
                'Tumblr', 'Twitter', 'WordPress')
KINDS = SOURCE_KINDS# + ('Response', 'BlogPost', 'Publish', 'BlogWebmention')
FEATURES = ('listen', 'publish', 'webmention')
EXCLUDE_PROPS = ('auth_entity', 'domains', 'domain_urls', 'features',
                 'feed_item', 'last_activities_cache_json', 'published',
                 'site_info', 'superfeedr_secret')

# maps string kind to csv.DictWriter
writers = {}

# maps string kind to list of entities (property dicts)
all_entities = collections.defaultdict(list)


#
# read app engine datastore admin backup files
#
# expects that they're named KIND-output-X-attempt-Y
# assumes only one attempt for all files
for kind in KINDS:
  for filename in glob.glob(kind + '-output-*-attempt-*'):
    print filename
    # sys.stdout.write('.')
    # sys.stdout.flush()

    with open(filename, 'rb') as raw:
      reader = records.RecordsReader(raw)
      for record in reader:
        entity_proto = entity_pb.EntityProto(contents=record)
        props = datastore.Entity.FromPb(entity_proto)
        all_entities[kind].append(props)
        props = {k: ' '.join(v.splitlines()).encode('utf-8')
                 if isinstance(v, basestring) else v
                 for k, v in props.items() if k not in EXCLUDE_PROPS}

        writer = writers.get(kind)
        if not writer:
          writer = writers[kind] = csv.DictWriter(open(kind + '.tsv', 'w'),
                                                  sorted(props.keys()),
                                                  dialect='excel-tab',
                                                  extrasaction='ignore')
          writer.writeheader()
        writer.writerow(props)

#
# generate time series growth data for number of users, wms sent, etc. by day
#

# sort chronologically
for values in all_entities.values():
  values.sort(key=lambda e: e['created'])

# domains that have successfully received a webmention
domains = set()

# walk days from launch to now, accumulate counts per day
with open('growth.tsv', 'w') as file:
  writer = csv.writer(file, dialect='excel-tab')
  columns = KINDS + FEATURES + ('links', 'webmentions', 'domains')
  writer.writerow(('created',) + columns)

  # maps string column to count
  counts = {c: 0 for c in columns}
  date = datetime.date(2013, 12, 1)
  while date < datetime.date.today():
    for kind in KINDS:
      entities = all_entities[kind]
      while entities and entities[0]['created'].date() == date:
        counts[kind] += 1
        e = entities.pop(0)

        if kind in SOURCE_KINDS:
          for f in e.get('features', []):
            counts[f] += 1
        elif kind in ('Response', 'BlogPost'):
          sent = e.get('sent', [])
          counts['webmentions'] += len(sent)
          links = list(itertools.chain(*[e.get(field, []) for field in
                       'sent', 'unsent', 'error', 'failed', 'skipped']))
          counts['links'] += len(links)
          domains.update(urlparse.urlparse(l).netloc for l in sent)
          counts['domains'] = len(domains)

    writer.writerow([date] + [counts[c] for c in columns])
    date += datetime.timedelta(days=1)

    if date.day == 1:
      sys.stdout.write('.')
      sys.stdout.flush()

  for kind, entities in all_entities.items():
    if entities:
      print '%d %s entities left over! e.g. %s' % (len(entities), kind, entities[0])
