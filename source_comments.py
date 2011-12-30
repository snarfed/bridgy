# """Datastore model classes for source comments.
# """

# __author__ = ['Ryan Barrett <bridgy@ryanb.org>']

# from google.appengine.ext import db

# import sources


# class SourceComment(object):
#   """An original comment in a source site.

#   The key name is the comment's uid in the source site, e.g. post_fbid in
#   Facebook.

#   A SourceComment is parent to all of its corresponding DestComments."""
#   site = db.ReferenceProperty(reference_class=sources.Source, required=True)
#   created = db.DateTimeProperty(auto_now_add=True, required=True)
#   source_url = db.LinkProperty()


# class FacebookSourceComment(db.Model, SourceComment):
#   """A comment in facebook.

#   The key name is the comment's post_fbid:
#   http://developers.facebook.com/docs/reference/fql/comment/
#   """
#   pass
