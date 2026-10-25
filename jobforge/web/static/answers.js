// Client-side pre-check for the answer-bank form. No frameworks, no build step.
// The server validates again; this just avoids a round trip for obvious mistakes.
(function () {
  "use strict";

  var form = document.getElementById("answer-form");
  var note = document.getElementById("answer-form-note");
  if (!form || !note) return;

  var keyInput = form.querySelector('input[name="key"]');
  var valueInput = form.querySelector('textarea[name="value"]');
  var maxKey = parseInt(form.querySelector('input[name="key"]').getAttribute("maxlength"), 10);
  var maxValue = parseInt(form.querySelector('textarea[name="value"]').getAttribute("maxlength"), 10);

  function hasForbidden(text) {
    for (var i = 0; i < text.length; i++) {
      var code = text.charCodeAt(i);
      if (text[i] === "<" || text[i] === ">" || code < 32 || code === 127) return true;
    }
    return false;
  }

  form.addEventListener("submit", function (event) {
    var key = keyInput.value.trim();
    var value = valueInput.value;
    var problem = "";

    if (key.length < 1 || key.length > maxKey) {
      problem = "Question substring must be 1-" + maxKey + " characters.";
    } else if (value.length < 1 || value.length > maxValue) {
      problem = "Answer must be 1-" + maxValue + " characters.";
    } else if (hasForbidden(key) || hasForbidden(value)) {
      problem = "Angle brackets and control characters are not allowed.";
    }

    if (problem) {
      event.preventDefault();
      note.textContent = problem;
      note.style.color = "var(--bad)";
      (key.length < 1 ? keyInput : valueInput).focus();
      return;
    }
    note.textContent = "Saving...";
  });
})();
