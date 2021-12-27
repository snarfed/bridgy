"""Renders admin pages for ops and other management tasks.

Currently just /admin/responses, which shows active responses with tasks that
haven't completed yet.
"""
import datetime
import itertools
import logging

from flask import render_template, request
from google.cloud import ndb
from google.cloud.ndb.stats import KindStat, KindPropertyNamePropertyTypeStat
from oauth_dropins.webutil import logs
from oauth_dropins.webutil.util import json_dumps, json_loads

from flask_app import app
import models
import util
# Import source class files so their metaclasses are initialized.
from models import BlogPost, Response, Source
import blogger, flickr, github, instagram, mastodon, medium, tumblr, twitter, wordpress_rest

NUM_ENTITIES = 10

# Result of this query in BigQuery:
# SELECT count(*) FROM `brid-gy.datastore.Response` WHERE updated < timestamp('2020-11-01T00:00:00Z')
ARCHIVED_RESPONSES = 19988618

# Result of this query in BigQuery:
# SELECT SUM(ARRAY_LENGTH(sent) + ARRAY_LENGTH(unsent) + ARRAY_LENGTH(error) + ARRAY_LENGTH(failed) + ARRAY_LENGTH(skipped))
# FROM `brid-gy.datastore.Response`
# WHERE updated < timestamp('2020-11-01T00:00:00Z')
ARCHIVED_LINKS = 3706943

# Result of this query in BigQuery:
# SELECT SUM(ARRAY_LENGTH(sent))
# FROM `brid-gy.datastore.Response`
# WHERE updated < timestamp('2020-11-01T00:00:00Z')
ARCHIVED_SENT_LINKS = 1655743


@app.route('/admin/responses')
def responses():
  """Find the most recently attempted responses and blog posts with error URLs."""
  entities = []

  for cls in (Response,):  # BlogPost
    for e in cls.query().order(-cls.updated):
      if (len(entities) >= NUM_ENTITIES or
          e.updated < datetime.datetime.now() - datetime.timedelta(hours=1)):
        break
      elif (not e.error and not e.unsent) or e.status == 'complete':
        continue

      e.links = [util.pretty_link(u, new_tab=True) for u in e.error + e.failed]
      if e.key.kind() == 'Response':
        e.response = json_loads(e.response_json)
        e.activities = [json_loads(a) for a in e.activities_json]
      else:
        e.response = {'content': '[BlogPost]'}
        e.activities = [{'url': e.key.id()}]

      entities.append(e)

  return render_template('admin_responses.html', responses=entities, logs=logs)


@app.route('/admin/sources')
def sources():
  """Find sources whose last poll errored out."""
  CLASSES = (flickr.Flickr, github.GitHub, twitter.Twitter,
             instagram.Instagram, mastodon.Mastodon)
  queries = [cls.query(Source.status == 'enabled',
                       Source.poll_status == 'error',
                       Source.rate_limited.IN((False, None)),
                       Source.features == 'listen',
                      ).fetch_async(NUM_ENTITIES)
             for cls in CLASSES]

  return render_template(
    'admin_sources.html',
    sources=itertools.chain(*[q.get_result() for q in queries]),
    logs=logs,
  )


@app.route('/admin/mark_complete', methods=['POST'])
def mark_complete():
  entities = ndb.get_multi(ndb.Key(urlsafe=u)
                           for u in request.values.getlist('key'))
  for e in entities:
    e.status = 'complete'
  ndb.put_multi(entities)
  return util.redirect('/admin/responses')


@app.route('/admin/disable', methods=['POST'])
def disable():
  source = util.load_source()
  logging.info(f'Disabling {source.label()}')
  source.status = 'disabled'
  source.put()
  return util.redirect(source.bridgy_path())


@app.route('/admin/stats')
def stats():
  """Collect and report misc lifetime stats.

  https://developers.google.com/appengine/docs/python/ndb/admin#Statistics_queries

  Used to be on the front page, dropped them during the Flask port in August 2021.
  """
  def count(query):
    stat = query.get()  # no datastore stats when running locally
    return stat.count if stat else 0

  def kind_count(kind):
    return count(KindStat.query(KindStat.kind_name == kind))

  num_users = sum(kind_count(cls.__name__) for cls in models.sources.values())
  response_count = kind_count('Response')
  link_counts = {
    property: sum(count(KindPropertyNamePropertyTypeStat.query(
      KindPropertyNamePropertyTypeStat.kind_name == kind,
      KindPropertyNamePropertyTypeStat.property_name == property,
      # specify string because there are also >2M Response entities with null
      # values for some of these properties, as opposed to missing altogether,
      # which we don't want to include.
      KindPropertyNamePropertyTypeStat.property_type == 'String'))
                  for kind in ('BlogPost', 'Response'))
    for property in ('sent', 'unsent', 'error', 'failed', 'skipped')}

  return render_template('admin_stats.html', **{
    # add comma separator between thousands
    k: f'{v:,}' for k, v in {
      'users': num_users,
      'responses': response_count + ARCHIVED_RESPONSES,
      'responses_stored': response_count,
      'links': sum(link_counts.values()) + ARCHIVED_LINKS,
      'webmentions': link_counts['sent'] + kind_count('BlogPost') + ARCHIVED_SENT_LINKS,
      'publishes': kind_count('Publish'),
      'blogposts': kind_count('BlogPost'),
      'webmentions_received': kind_count('BlogWebmention'),
    }.items()})
