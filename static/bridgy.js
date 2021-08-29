/** Misc JavaScript.
 */

// Used for the "More..." link.
function toggle(id) {
  var elem = document.getElementById(id);
  elem.style.display = (elem.style.display == 'none') ? 'block' : 'none';
}

// Hide flashed messages. CSS transition in style.css fades them slowly.
window.onload = function () {
  for (const p of document.getElementsByClassName('message')) {
    window.setTimeout(function() {
      p.style.opacity = 0;  // uses delayed transition
    }, 5 /* ms; needed for transition after setting display to non-none */);

    window.setTimeout(function() {
      p.style.display = 'none';
    }, (20 + 5) * 1000 /* ms; match transition duration + delay */);
  }
}

// AJAX publish previews on user pages.
function do_preview(site) {
  var msgs = document.getElementById('messages');
  if (msgs) {
      msgs.style.display = 'none';
  }

  var preview = document.getElementById('preview');
  var req = new XMLHttpRequest();
  req.onload = function() {
    if (this.status == 200) {
      preview = document.getElementById('preview');
      preview.innerHTML = this.responseText;
      preview.scrollIntoView({behavior: 'smooth', block: 'nearest'});
      // trigger re-render of twitter embed
      if (typeof(twttr) != 'undefined') {
        twttr.widgets.load();
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
