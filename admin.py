"""Renders admin pages for ops and other management tasks.

Currently just /admin/responses, which shows active responses with tasks that
haven't completed yet.
"""
import datetime
import itertools

from oauth_dropins.webutil import handlers
from models import BlogPost, Response, Source
import util

from google.cloud import ndb
from oauth_dropins.webutil import logs
from oauth_dropins.webutil.util import json_dumps, json_loads
import webapp2

# Import source class files so their metaclasses are initialized.
import blogger, flickr, github, instagram, mastodon, medium, pixelfed, tumblr, twitter, wordpress_rest


class ResponsesHandler(handlers.TemplateHandler):
  """Find the most recently attempted responses and blog posts with error URLs."""
  NUM_ENTITIES = 10

  def template_file(self):
    return 'admin_responses.html'

  def template_vars(self):
    entities = []

    for cls in (Response,):  # BlogPost
      for e in cls.query().order(-cls.updated):
        if (len(entities) >= self.NUM_ENTITIES or
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

    return {'responses': entities, 'logs': logs}


class SourcesHandler(handlers.TemplateHandler):
  """Find sources whose last poll errored out."""
  NUM_SOURCES = 10

  def template_file(self):
    return 'admin_sources.html'

  def template_vars(self):
    CLASSES = (flickr.Flickr, github.GitHub, twitter.Twitter,
               instagram.Instagram, mastodon.Mastodon, pixelfed.Pixelfed)
    queries = [cls.query(Source.status == 'enabled',
                         Source.poll_status == 'error',
                         Source.rate_limited.IN((False, None)),
                         Source.features == 'listen',
                        ).fetch_async(self.NUM_SOURCES)
               for cls in CLASSES]
    return {
      'sources': itertools.chain(*[q.get_result() for q in queries]),
      'logs': logs,
    }


class MarkCompleteHandler(util.Handler):

  def post(self):
    entities = ndb.get_multi(ndb.Key(urlsafe=u)
                             for u in self.request.params.getall('key'))
    for e in entities:
      e.status = 'complete'
    ndb.put_multi(entities)
    self.redirect('/admin/responses')


ROUTES =[
  ('/admin/responses', ResponsesHandler),
  ('/admin/sources', SourcesHandler),
  ('/admin/mark_complete', MarkCompleteHandler),
]
