"""Renders admin pages for ops and other management tasks.

Currently just /admin/responses, which shows active responses with tasks that
haven't completed yet.
"""
import datetime
import itertools


from flask import Flask, redirect, render_template
from google.cloud import ndb
from oauth_dropins.webutil import flask_util, logs
from oauth_dropins.webutil.util import json_dumps, json_loads

from models import BlogPost, Response, Source
import util
# Import source class files so their metaclasses are initialized.
import blogger, flickr, github, instagram, mastodon, medium, tumblr, twitter, wordpress_rest

NUM_ENTITIES = 10


from oauth_dropins.webutil import appengine_config, appengine_info
app = Flask('bridgy')
app.template_folder = './templates'
app.config.from_mapping(
    ENV='development' if appengine_info.DEBUG else 'PRODUCTION',
    CACHE_TYPE='SimpleCache',
    SECRET_KEY=util.read('flask_secret_key'),
    JSONIFY_PRETTYPRINT_REGULAR=True,
)
app.url_map.converters['regex'] = flask_util.RegexConverter
app.after_request(flask_util.default_modern_headers)
app.register_error_handler(Exception, flask_util.handle_exception)
app.before_request(flask_util.canonicalize_domain(
  util.OTHER_DOMAINS, util.PRIMARY_DOMAIN))

app.wsgi_app = flask_util.ndb_context_middleware(
    app.wsgi_app, client=appengine_config.ndb_client)


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
  return redirect('/admin/responses')
