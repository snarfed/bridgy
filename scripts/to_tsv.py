#!/usr/local/bin/python
"""Generate growth data since launch using datastore admin backup files.

TODO: port to BigQuery queries.
https://cloud.google.com/bigquery/loading-data-cloud-datastore

Outputs TSV files for each entity kind and a growth.tsv file with daily counts
for all kinds and other features. It's ugly, inadequately commented, poorly
tested, etc. Don't use it for anything remotely important!

Warning, takes >6h to run (on e.g. a 2014 MBP) and GBs of memory!

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

To download the files:
gsutil cp -r gs://brid-gy.appspot.com/weekly/datastore_backup_full_YYYY_MM_DD_\* .
"""
from __future__ import print_function

from future import standard_library
standard_library.install_aliases()
from past.builtins import basestring
import collections
import csv
import datetime
import glob
import itertools
import logging
import sys
import urllib.parse

sys.path.append('/usr/local/google_appengine')

from google.appengine.api.files import records
from google.appengine.datastore import entity_pb
from google.appengine.api import datastore
from google.appengine.api import datastore_errors

SOURCE_KINDS = (
  'Blogger',
  'FacebookPage',
  'Flickr',
  'GooglePlusPage',
  'Medium',
  'Instagram',
  'Tumblr',
  'Twitter',
  'WordPress',
)
KINDS = SOURCE_KINDS + ('Response', 'BlogPost', 'Publish', 'BlogWebmention')
FEATURES = ('listen', 'publish', 'webmention')
INCLUDE_PROPS = {'features', 'sent', 'unsent', 'error', 'failed', 'skipped', 'links', 'domains', 'created', 'updated'}

# maps string kind to list of entities (property dicts)
all_entities = collections.defaultdict(list)


#
# read app engine datastore admin backup files
#
for filename in glob.glob('datastore_backup_*/*/*'):
  print(filename)

  with open(filename, 'rb') as raw:
    reader = records.RecordsReader(raw)
    for record in reader:
      try:
        entity_proto = entity_pb.EntityProto(contents=record)
        entity = datastore.Entity.FromPb(entity_proto)
      except datastore_errors.Error:
        logging.error('!!! Skipped an entity !!! %s' % entity.key().to_path(),
                      exc_info=True)
        continue
      kind = entity.kind()
      if kind not in KINDS:
        continue
      props = {k: ' '.join(v.splitlines()).encode('utf-8')
               if isinstance(v, basestring) else v
               for k, v in entity.items() if k in INCLUDE_PROPS}
      all_entities[kind].append(props)


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
                       ('sent', 'unsent', 'error', 'failed', 'skipped')]))
          counts['links'] += len(links)
          domains.update(urllib.parse.urlparse(l).netloc for l in sent)
          counts['domains'] = len(domains)

    writer.writerow([date] + [counts[c] for c in columns])
    date += datetime.timedelta(days=1)

    if date.day == 1:
      print(date)

  for kind, entities in list(all_entities.items()):
    if entities:
      print('%d %s entities left over! e.g. %s' % (len(entities), kind, entities[0]))
