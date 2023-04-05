/** Misc JavaScript.
 */

// Used for the "More..." link.
function toggle(id) {
  var elem = document.getElementById(id);
  elem.style.display = (elem.style.display == 'none') ? 'block' : 'none';
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
    } else {
      this.onerror();
    }
  };
  req.onerror = function() {
      preview.innerHTML =
        '<span title="Error" class="glyphicon glyphicon-exclamation-sign"></span> ' +
        this.responseText;
      preview.classList = 'row error';
  }

  preview.innerHTML = '<img src="/static/spinner.gif" width="30" />';
  params = new URLSearchParams(new FormData(document.getElementsByName('preview')[0]));
  req.open('post', '/publish/preview?' + params.toString());
  req.send();
}
