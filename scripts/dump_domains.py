#!/usr/local/bin/python
"""Dump the domains of all web site links in user profiles.

...along with the number of different users who have each domain in their profile.

From https://github.com/snarfed/bridgy/issues/490#issuecomment-143572623
"""

import collections
import models
import blogger
import facebook
import flickr
import googleplus
import instagram
import tumblr
import twitter
import wordpress_rest

domains = collections.defaultdict(int)  # maps domain to # of users
for cls in models.sources.values():
  for src in cls.query(cls.domains > ''):
    for domain in src.domains:
      print domain
      domains[domain] += 1

with open('domains.txt', 'w') as f:
  f.write('domain,num_users\n')
  f.write('\n'.join(str(item) for item in reversed(sorted(
    '%s,%s' % (item[1], item[0]) for item in domains.items()))))
