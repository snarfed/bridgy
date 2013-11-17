"""Common handlers, e.g. post and comment permalinks.

URL path format is /SITE/(post|comment)/USER_ID/OBJECT_ID, e.g.:
  /facebook/post/212038/10100823411094363
  /twitter/comment/snarfed_org/10100823411094363
"""

import json
import urlparse

from activitystreams import microformats2
import appengine_config
import util
import webapp2
from webutil import handlers


class ObjectHandler(webapp2.RequestHandler):
  """Fetches a post or comment and serves it as microformat2 HTML or JSON.
  """
  handle_exception = handlers.handle_exception

  @staticmethod
  def using(source_cls, get_object_fn):
    class Subclass(ObjectHandler):
      pass
    Subclass.source_cls = source_cls
    Subclass.get_object_fn = get_object_fn
    return Subclass

  def get(self, key_name, id):
    src = self.source_cls.get_by_key_name(key_name)
    if not src:
      self.abort(400, 'User %s not found' % key_name)

    format = self.request.get('format', 'html')
    if format not in ('html', 'json'):
      self.abort(400, 'Invalid format %s, expected html or json' % format)

    obj = getattr(src, self.get_object_fn)(id)

    self.response.headers['Access-Control-Allow-Origin'] = '*'
    if format == 'html':
      self.response.headers['Content-Type'] = 'text/html'
      self.response.out.write("""\
<!DOCTYPE html>
<html>
%s
</html>
""" % microformats2.object_to_html(obj))
    elif format == 'json':
      self.response.headers['Content-Type'] = 'application/json'
      self.response.out.write(json.dumps(microformats2.object_to_json(obj),
                                         indent=2))
