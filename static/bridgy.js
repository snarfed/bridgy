/** Misc JavaScript.
 */

// Used for the disabled account icon on user pages, to reconnect.
document.addEventListener('click', (event) => {
  if (event.target.closest('[data-submit-first-form]')) {
    event.preventDefault();
    document.forms[0].submit();
  }
});

document.addEventListener('submit', (event) => {
  if (event.target.name == 'preview') {
    event.preventDefault();
    do_preview();
  }
});

// AJAX publish previews on user pages.
function do_preview() {
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
