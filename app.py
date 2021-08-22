"""Bridgy user-facing app invoked by gunicorn in app.yaml.

Import all modules that define views in the app so that their URL routes get
registered.
"""
from flask_app import app

import admin, browser, handlers, pages, superfeedr, webmention

# sources
import blogger, facebook, flickr, github, indieauth, instagram, mastodon, meetup, medium, reddit, tumblr, twitter, wordpress_rest
