"""Renders admin pages for ops and other management tasks.

Currently just /admin/responses, which shows active responses with tasks that
haven't completed yet.
"""

import datetime
import json

import appengine_config
from activitystreams.oauth_dropins.webutil import handlers
import facebook
import googleplus
from models import Response
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

    # Find the most recently propagated responses with error URLs
    for r in Response.query().order(-Response.updated):
      if (len(responses) >= self.NUM_RESPONSES or
          r.updated < datetime.datetime.now() - datetime.timedelta(hours=1)):
        break
      elif not r.error or r.status == 'complete':
        continue

      # r.source = r.source.get()
      r.links = [util.pretty_link(u, new_tab=True) for u in r.error + r.failed]
      r.response = json.loads(r.response_json)
      r.activities = [json.loads(a) for a in r.activities_json]

      responses.append(r)

    responses.sort(key=lambda r: (r.source, r.activities, r.response))
    return {'responses': responses}


class MarkCompleteHandler(util.Handler):
  def post(self):
    responses = ndb.get_multi(ndb.Key(urlsafe=u)
                              for u in self.request.params.getall('key'))
    for r in responses:
      r.status = 'complete'
    ndb.put_multi(responses)
    self.redirect('/admin/responses')


application = webapp2.WSGIApplication([
    ('/admin/responses', ResponsesHandler),
    ('/admin/mark_complete', MarkCompleteHandler),
    ], debug=appengine_config.DEBUG)


if __name__ == '__main__':
  main()
