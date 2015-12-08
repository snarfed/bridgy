"""Unit tests for webmention.py.
"""

import testutil
import webmention
import webapp2


class WebmentionHandlerTest(testutil.HandlerTest):

    def test_tumblr_special_case(self):
        """fetch_mf2 carves out a special case for Tumblr posts that don't
        have mf2, but do have predictable HTML classes. Make sure it
        is applied.
        """
        self.expect_requests_get('https://foo.tumblr.com/bar', """
<!DOCTYPE html>
<html>
<head></head>
<body>
  <div id="content">
    <div class="post">
      <div class="copy">blah</div>
      <div class="photo-wrapper">
        <img src="http://baz.org/img.jpg"/>
      </div>
    </div>
  </div>
</body>
</html>
""")
        self.mox.ReplayAll()

        handler = webmention.WebmentionHandler()
        handler.response = webapp2.Response()

        r, p = handler.fetch_mf2('https://foo.tumblr.com/bar')

        self.assertTrue(p.get('items') and len(p.get('items')) == 1)
        self.assert_equals({
            'type': ['h-entry'],
            'properties': {
                'name': ['blah'],
                'content': [{
                    'html': 'blah',
                    'value': 'blah',
                }],
                'photo': ['http://baz.org/img.jpg'],
            }
        }, p.get('items')[0])

    def test_tumblr_special_case_does_not_override_mf1(self):
        """Tumblr's special case should not add "h-entry" on a class
        that already has mf1 microformats on it (or it will cause the parser
        to ignore the mf2 properties.
        """

        self.expect_requests_get('https://foo.wordpress.com/bar', """
<!DOCTYPE html>
<html>
<head></head>
<body>
  <div id="content">
    <div class="post hentry">
      <div class="entry-content">blah</div>
      <img class="photo" src="http://baz.org/img.jpg"/>
    </div>
  </div>
</body>
</html>
""")
        self.mox.ReplayAll()

        handler = webmention.WebmentionHandler()
        handler.response = webapp2.Response()

        r, p = handler.fetch_mf2('https://foo.wordpress.com/bar')

        self.assertTrue(p.get('items') and len(p.get('items')) == 1)
        self.assert_equals({
            'type': ['h-entry'],
            'properties': {
                'name': ['blah'],
                'content': [{
                    'html': 'blah',
                    'value': 'blah',
                }],
                'photo': ['http://baz.org/img.jpg'],
            }
        }, p.get('items')[0])
