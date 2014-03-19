"""Cron jobs. Currently just minor cleanup tasks.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import itertools
import logging

import appengine_config

import handlers
from models import Source
import util
import webapp2

TOO_OLD = datetime.timedelta(hours=2)


class ReplacePollTasks(webapp2.RequestHandler):
  """Finds sources missing their poll tasks and adds new ones."""

  def get(self):
    now = datetime.datetime.now()
    queries = [cls.query(Source.features == 'listen', Source.status == 'enabled')
               for cls in handlers.SOURCES.values()]
    for source in itertools.chain(*queries):
      age = now - source.last_polled
      if age > TOO_OLD:
        logging.info('%s last polled %s ago. Adding new poll task.',
                     source.dom_id(), age)
        util.add_poll_task(source)


application = webapp2.WSGIApplication([
    ('/cron/replace_poll_tasks', ReplacePollTasks),
    ], debug=appengine_config.DEBUG)
