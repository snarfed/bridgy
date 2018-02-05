"""Mapreduces. Right now just maintennance tasks, no production code.

https://developers.google.com/appengine/docs/python/dataprocessing/

Best guides I found for writing a datastore mapreduce and tuning it in prod:
https://code.google.com/p/appengine-mapreduce/wiki/GettingStartedInPython
http://code.google.com/p/appengine-mapreduce/wiki/InstancesQueuesShardsAndSlices
"""
from __future__ import unicode_literals

import gc
import json

from mapreduce import operation as op
import util


def prune_activity_json(response):
  """Prune the Response.activity_json property.

  Background: https://github.com/snarfed/bridgy/issues/68
  """
  response.activity_json = json.dumps(util.prune_activity(
      json.loads(response.activity_json)))
  # helps avoid hitting the instance memory limit
  gc.collect()
  yield op.db.Put(response)
