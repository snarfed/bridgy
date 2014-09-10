"""Renders admin pages for ops and other management tasks.

Currently just /admin/responses, which shows active responses with tasks that
haven't completed yet.
"""

import datetime
import itertools
import json

import appengine_config
from activitystreams.oauth_dropins.webutil import handlers
import facebook
import googleplus
from models import BlogPost, Response
import instagram
import twitter
import util

from google.appengine.ext import ndb
import webapp2


class ResponsesHandler(handlers.TemplateHandler):
  NUM_RESPONSES = 30

  def template_file(self):
    return 'templates/admin_responses.html'

  def template_vars(self):
    responses = []

    # Find the most recently attempted responses and blog posts with error URLs
    for e in itertools.chain(*(cls.query().order(-cls.updated)
                               for cls in (BlogPost, Response))):
      if (len(responses) >= self.NUM_RESPONSES or
          e.updated < datetime.datetime.now() - datetime.timedelta(hours=1)):
        break
      elif (not e.error and not e.unsent) or e.status == 'complete':
        continue

      e.links = [util.pretty_link(u, new_tab=True) for u in e.error + e.failed]
      if e.key.kind() == 'Response':
        e.response = json.loads(e.response_json)
        e.activities = [json.loads(a) for a in e.activities_json]
      else:
        e.response = {'content': '[BlogPost]'}
        e.activities = [{'url': e.key.id()}]

      responses.append(e)

    responses.sort(key=lambda e: (e.source, e.activities, e.response))
    return {'responses': responses}


class MarkCompleteHandler(util.Handler):
  def post(self):
    entities = ndb.get_multi(ndb.Key(urlsafe=u)
                             for u in self.request.params.getall('key'))
    for e in entities:
      e.status = 'complete'
    ndb.put_multi(responses)
    self.redirect('/admin/responses')


application = webapp2.WSGIApplication([
    ('/admin/responses', ResponsesHandler),
    ('/admin/mark_complete', MarkCompleteHandler),
    ], debug=appengine_config.DEBUG)


if __name__ == '__main__':
  main()
