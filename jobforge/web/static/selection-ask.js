// Selection-to-agent: select text on a job description or an agent answer,
// ask a question, and continue in the main agent composer.
(function () {
  "use strict";

  var SURFACE = "[data-ask-surface]";
  var MAX_SELECTION = 4000;
  var pop = null;
  var selectionText = "";
  var selectionContext = "";
  var timer = 0;
  var input = null;

  function build() {
    if (pop) return pop;
    pop = document.createElement("div");
    pop.className = "selection-ask";
    pop.setAttribute("role", "dialog");
    pop.setAttribute("aria-label", "Ask JobForge about the selection");
    pop.hidden = true;

    var orb = document.createElement("span");
    orb.className = "agent-orb";
    orb.setAttribute("aria-hidden", "true");

    input = document.createElement("input");
    input.type = "text";
    input.placeholder = "Ask about this…";
    input.setAttribute("aria-label", "Question about selected text");
    input.autocomplete = "off";

    var button = document.createElement("button");
    button.type = "button";
    button.className = "primary";
    button.textContent = "Ask";

    pop.appendChild(orb);
    pop.appendChild(input);
    pop.appendChild(button);
    document.body.appendChild(pop);

    pop.addEventListener("mousedown", function (event) {
      if (event.target !== input) event.preventDefault();
    });
    button.addEventListener("click", submit);
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        submit();
      } else if (event.key === "Escape") {
        hide();
      }
    });
    return pop;
  }

  function hide() {
    if (pop) {
      pop.hidden = true;
      if (input) input.value = "";
    }
  }

  function surfaceFor(node) {
    var element = node && node.nodeType === 3 ? node.parentElement : node;
    return element && element.closest ? element.closest(SURFACE) : null;
  }

  function show(text, context, box) {
    build();
    selectionText = text;
    selectionContext = context;
    pop.hidden = false;
    var left = Math.min(
      Math.max(12, box.left + box.width / 2 - 150),
      window.innerWidth - 312
    );
    var top = box.top - 52;
    if (top < 12) top = box.bottom + 10;
    pop.style.left = left + "px";
    pop.style.top = top + "px";
    input.focus({ preventScroll: true });
  }

  function inspectSelection() {
    var selection = window.getSelection();
    if (!selection || selection.isCollapsed || !selection.rangeCount) {
      hide();
      return;
    }
    var text = selection.toString().trim();
    if (!text || text.length < 8) {
      hide();
      return;
    }
    var surface = surfaceFor(selection.anchorNode);
    if (!surface || !surface.contains(selection.focusNode)) {
      hide();
      return;
    }
    if (surface.closest("input, textarea, [contenteditable='true']")) {
      hide();
      return;
    }
    var range = selection.getRangeAt(0);
    var box = range.getBoundingClientRect();
    if (box.width === 0 && box.height === 0) {
      hide();
      return;
    }
    show(
      text.slice(0, MAX_SELECTION),
      surface.getAttribute("data-ask-context") || document.title,
      box
    );
  }

  function queueInspect(delay) {
    window.clearTimeout(timer);
    timer = window.setTimeout(inspectSelection, delay === undefined ? 180 : delay);
  }

  function draft() {
    var question = (input.value || "").trim();
    var prompt = question || "Explain this and connect it to my verified profile.";
    return (
      "Selected from " +
      selectionContext +
      ':\n"' +
      selectionText +
      '"\n\nQuestion: ' +
      prompt
    );
  }

  function fillComposer(text) {
    var form = document.getElementById("agent-form");
    var composer = document.getElementById("agent-input");
    if (!form || !composer) return false;
    composer.value = text;
    composer.focus();
    composer.setSelectionRange(text.length, text.length);
    form.scrollIntoView({ behavior: "smooth", block: "center" });
    return true;
  }

  function submit() {
    var text = draft();
    hide();
    if (fillComposer(text)) return;

    fetch("/api/agent/threads", { method: "POST" })
      .then(function (response) {
        if (!response.ok) throw new Error("Could not start a conversation");
        return response.json();
      })
      .then(function (data) {
        var thread = data.thread;
        if (!thread || !thread.id) throw new Error("No conversation returned");
        try {
          window.sessionStorage.setItem("jf_agent_draft", text);
        } catch (error) {
          // sessionStorage may be disabled; the agent page still opens.
        }
        window.location = "/agent?thread=" + encodeURIComponent(thread.id);
      })
      .catch(function (error) {
        window.alert(error.message || "Could not start a conversation");
      });
  }

  document.addEventListener("selectionchange", function () {
    queueInspect(180);
  });
  document.addEventListener("pointerup", function (event) {
    if (pop && pop.contains(event.target)) return;
    queueInspect(20);
  });
  document.addEventListener("pointerdown", function (event) {
    if (pop && pop.contains(event.target)) return;
    hide();
  });
  document.addEventListener("scroll", hide, true);
  window.addEventListener("resize", hide);
})();
