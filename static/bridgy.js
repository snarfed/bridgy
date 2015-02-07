/** Misc JavaScript.
 */

// Used for the "More..." link.
function toggle(id) {
  var elem = document.getElementById(id);
  elem.style.display = (elem.style.display == 'none') ? 'block' : 'none';
}

// Extract toast style messages from URL fragments and render them at the top of
// the page.
window.onload = function () {
  if (window.location.hash.substr(0, 2) == '#!') {
    var p = document.getElementById('message');
    p.style.display = 'inline';
    p.innerHTML = decodeURIComponent(
      window.location.hash.substr(2))  // strip leading #!
        .replace('\n', '<br />');
    window.location.hash = '';
  }
}

// AJAX publish previews on user pages.
function do_preview(site) {
  var url = document.getElementById('source-url').value.trim();
  if (url.length == 0) {
    window.alert('Please enter a URL.');
    return;
  } else if (!url.startsWith('http')) {
    url = 'http://' + url;
  }

  var preview = document.getElementById('preview');
  var req = new XMLHttpRequest();
  req.onload = function() {
    if (this.status == 200) {
      document.getElementById('preview').innerHTML = this.responseText;
      // trigger re-renders of twitter and facebook embeds
      if (typeof(twttr) != 'undefined') {
        twttr.widgets.load();
      }
      if (typeof(FB) != 'undefined') {
        FB.XFBML.parse();
      }
      if (typeof(instgrm) != 'undefined') {
        instgrm.Embeds.process()
      }
    } else {
      this.onerror();
    }
  };
  req.onerror = function() {
      preview.innerHTML =
        '<span title="Error" class="glyphicon glyphicon-exclamation-sign"></span> ' +
        this.responseText;
      preview.class = 'error';
  }

  preview.innerHTML = '<img src="/static/spinner.gif" width="30" />';
  req.open('post', '/publish/preview?source=' + encodeURIComponent(url) +
    '&target=http://brid.gy/publish/' + site +
    '&bridgy_omit_link=' + !document.getElementById('include-link-checked').checked +
    '&source_key=' + document.getElementById('source_key').value);
  req.send();
}

// Polyfill String.startsWith() since it's only supported in Firefox right now.
if (!String.prototype.startsWith) {
  Object.defineProperty(String.prototype, 'startsWith', {
    enumerable: false,
    configurable: false,
    writable: false,
    value: function (searchString, position) {
      position = position || 0;
      return this.indexOf(searchString, position) === position;
    }
  });
}
