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
  }

  var preview = document.getElementById('preview');
  var req = new XMLHttpRequest();
  req.onload = function() {
    if (this.status == 200) {
      document.getElementById('preview').innerHTML = this.responseText;
      // trigger re-renders of twitter and facebook embeds
      twttr.widgets.load();
      if (typeof(FB) != 'undefined') {
        FB.XFBML.parse();
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
  req.open('post', '/publish/preview?source=' + url +
           '&target=http://brid.gy/publish/' + site);
  req.send();
}

function send_preview(url) {
  var sent = document.getElementById('sent');
  var glyph = '<span title="Error" class="glyphicon glyphicon-exclamation-sign"></span> ';
  var req = new XMLHttpRequest();

  req.onload = function() {
    if (this.status == 200) {
      sent.innerHTML = 'Sent! <a href="' + JSON.parse(this.responseText).url +
                       '">Click here to view.</a>';
    } else if (this.status == 400) {
      sent.innerHTML = glyph + JSON.parse(this.responseText).error;
    } else {
      this.onerror();
    }
  };
  req.onerror = function() {
    sent.innerHTML = glyph + this.responseText;
  }

  sent.innerHTML = '<img src="/static/spinner.gif" width="30" />';
  req.open('post', url);
  req.send();
}
