// Small progressive enhancements. No frameworks, no build step.
(function () {
  "use strict";

  // Dismiss the flash message after a short delay.
  var flash = document.querySelector(".flash");
  if (flash) {
    setTimeout(function () {
      flash.style.transition = "opacity .4s";
      flash.style.opacity = "0";
      setTimeout(function () { flash.remove(); }, 450);
    }, 6500);
  }

  // Confirm destructive/irreversible actions.
  document.querySelectorAll('form[action$="/hide"]').forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm("Hide this job from the feed?")) event.preventDefault();
    });
  });

  // "/" focuses the search box.
  document.addEventListener("keydown", function (event) {
    if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
    var target = event.target;
    if (target && ["INPUT", "TEXTAREA", "SELECT"].indexOf(target.tagName) !== -1) return;
    var search = document.querySelector('input[type="search"]');
    if (search) {
      event.preventDefault();
      search.focus();
    }
  });
})();
