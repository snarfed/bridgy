"""Bridgy background app invoked by gunicorn in background.yaml.

Import all modules that define views in the background app so that their URL
routes get registered.
"""
from flask_background import app
import cron, tasks
