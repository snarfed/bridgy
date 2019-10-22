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
    p.style.display = 'block';

    p.innerHTML = decodeURIComponent(
      window.location.hash.substr(2))  // strip leading #!
        .replace('\n', '<br />');

    window.setTimeout(function() {
      p.style.opacity = 0;  // uses delayed transition
    }, 5 /* ms; needed for transition after setting display to non-none */);

    window.setTimeout(function() {
      p.style.display = 'none';
    }, (20 + 5) * 1000 /* ms; match transition duration + delay */);

    window.location.hash = '';
  }
}

// AJAX publish previews on user pages.
function do_preview(site) {
  document.getElementById('messages').style.display = 'none';

  var preview = document.getElementById('preview');
  var req = new XMLHttpRequest();
  req.onload = function() {
    if (this.status == 200) {
      preview = document.getElementById('preview');
      preview.innerHTML = this.responseText;
      preview.scrollIntoView({behavior: 'smooth', block: 'nearest'});
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
  params = new URLSearchParams(new FormData(document.getElementsByName('preview')[0]));
  req.open('post', '/publish/preview?' + params.toString());
  req.send();
}

// used in /admin/responses
function maybeShowInputs(event) {
  if (String.fromCharCode(event.charCode) == "x") {
    elems = document.getElementsByTagName("input");
    for (i = 0; i < elems.length; i++) {
      elems[i].style.display = "inline";
    }
  }
}

// used in /admin/responses
function selectAll() {
  checked = document.getElementById("all").checked;
  elems = document.getElementsByTagName("input");
  for (i = 0; i < elems.length; i++) {
    elems[i].checked = checked;
  }
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
