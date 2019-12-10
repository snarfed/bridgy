#!/usr/local/bin/python
"""Dump all domains in user profiles and that we've sent webmentions to.

Profile domains include the number of different users who have each domain in
their profile.

Started from https://github.com/snarfed/bridgy/issues/490#issuecomment-143572623
"""
import collections
import urllib.parse

import models
from models import Response
import blogger, flickr, github, instagram, mastodon, medium, tumblr, twitter, wordpress_rest


domains = collections.defaultdict(int)  # maps domain to # of users
for cls in models.sources.values():
  for src in cls.query(cls.domains > ''):
    for domain in src.domains:
      print(domain)
      domains[domain] += 1

with open('domains.txt', 'w') as f:
  f.write('domain,num_users\n')
  f.write('\n'.join(str(item) for item in reversed(sorted(
    '%s,%s' % (item[1], item[0]) for item in domains.items()))))

with open('domains_sent.txt', 'w') as f:
  url = ''
  while True:
    resp = Response.query(Response.sent > url).get(projection=['sent'])
    if not resp:
      break
    domain = None
    for sent in resp.sent:
      parsed = urllib.parse.urlparse(sent)
      if sent > url and (domain is None or parsed.netloc < domain):
        domain = parsed.netloc
    url = urllib.parse.urlunparse(parsed[:2] + ('', '', '', '')) + chr(ord('/') + 1)
    print(domain)
