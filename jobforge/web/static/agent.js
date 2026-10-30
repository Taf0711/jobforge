// Agent chat: no framework, no build step. Server-rendered history, JSON send.
(function () {
  "use strict";

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function reducedMotion() {
    return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  function revealText(node, text, done) {
    if (reducedMotion()) {
      node.textContent = text;
      if (done) done();
      return;
    }
    node.textContent = "";
    node.classList.add("streaming");
    var parts = String(text).split(/(\s+)/).filter(function (part) { return part.length; });
    var index = 0;
    var timer = window.setInterval(function () {
      var chunk = "";
      var words = 0;
      while (index < parts.length && words < 3) {
        chunk += parts[index];
        if (!/^\s+$/.test(parts[index])) words += 1;
        index += 1;
      }
      node.appendChild(document.createTextNode(chunk));
      if (index >= parts.length) {
        window.clearInterval(timer);
        node.classList.remove("streaming");
        if (done) done();
      }
    }, 12);
  }

  function renderToolTrace(trace, container) {
    if (!container || !trace || !trace.length) return;
    var details = el("details", "tool-trace");
    details.appendChild(el("summary", "", "Tool timeline · " + trace.length + " call(s)"));
    var list = el("ol", "tool-timeline");
    trace.forEach(function (item) {
      var li = el("li");
      li.appendChild(el("strong", "", item.name || "tool"));
      if (item.summary) li.appendChild(el("span", "muted", " · " + String(item.summary).slice(0, 180)));
      if (item.requires_write_permission) li.appendChild(el("span", "pill bad", "write blocked"));
      list.appendChild(li);
    });
    details.appendChild(list);
    container.appendChild(details);
  }

  function renderMessages(messages, container, animateLast) {
    if (!container) return;
    container.textContent = "";
    if (!messages || !messages.length) {
      container.appendChild(el("p", "empty", "No messages yet."));
      return;
    }
    var lastAssistant = -1;
    messages.forEach(function (message, index) {
      if (message.role === "assistant" && message.content) lastAssistant = index;
    });
    messages.forEach(function (message, index) {
      if (message.role === "user") {
        var user = el("div", "chat-msg user");
        user.appendChild(el("div", "chat-role", "You"));
        user.appendChild(el("div", "chat-body", message.content || ""));
        container.appendChild(user);
      } else if (message.role === "assistant") {
        if (message.content) {
          var assistant = el("div", "chat-msg assistant");
          assistant.appendChild(el("div", "chat-role", "JobForge"));
          var body = el("div", "chat-body");
          if (animateLast && index === lastAssistant) {
            revealText(body, message.content, function () {
              container.scrollTop = container.scrollHeight;
            });
          } else {
            body.textContent = message.content;
          }
          assistant.appendChild(body);
          container.appendChild(assistant);
        }
        if (message.tool_calls && message.tool_calls.length) {
          var names = message.tool_calls.map(function (call) {
            return call.name || "tool";
          }).join(", ");
          container.appendChild(el("div", "tool-row", "Used " + names));
        }
      } else if (message.role === "tool") {
        var details = el("details", "tool-call");
        var summary = el(
          "summary",
          "",
          (message.name || "tool") + (message.content ? " · " + String(message.content).slice(0, 90) : "")
        );
        details.appendChild(summary);
        details.appendChild(el("pre", "", String(message.content || "").slice(0, 6000)));
        container.appendChild(details);
      }
    });
    container.scrollTop = container.scrollHeight;
  }

  var form = document.getElementById("agent-form");
  var messages = document.getElementById("agent-messages");
  var input = document.getElementById("agent-input");
  var send = document.getElementById("agent-send");
  var status = document.getElementById("agent-status");
  var allow = document.getElementById("agent-allow-writes");

  function setStatus(text, busy) {
    if (!status) return;
    status.textContent = "";
    if (busy) {
      var orb = el("span", "agent-orb");
      orb.setAttribute("aria-hidden", "true");
      var shimmer = el("span", "agent-shimmer", text || "Working…");
      status.appendChild(orb);
      status.appendChild(shimmer);
    } else if (text) {
      status.textContent = text;
    }
  }

  function setBusy(busy, text) {
    if (send) send.disabled = busy;
    if (input) input.disabled = busy;
    setStatus(text, busy);
    if (form) form.classList.toggle("busy", !!busy);
  }

  if (form && input && send) {
    var threadId = form.getAttribute("data-thread-id");
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var text = input.value.trim();
      if (!text) return;
      setBusy(true, "Running the agent…");
      var stillWorking = window.setTimeout(function () {
        setStatus("Still working · tools and model latency", true);
      }, 8000);
      fetch("/api/agent/threads/" + encodeURIComponent(threadId) + "/messages", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: text,
          allow_writes: !!(allow && allow.checked)
        })
      })
        .then(function (response) {
          return response.json().then(function (data) {
            if (!response.ok) throw new Error(data.error || "Agent request failed");
            return data;
          });
        })
        .then(function (data) {
          window.clearTimeout(stillWorking);
          renderMessages(data.messages, messages, true);
          renderToolTrace(data.tool_trace, messages);
          input.value = "";
          var stopped = data.stopped_reason ? " · stopped: " + data.stopped_reason : "";
          setBusy(false, "Done · " + (data.steps || 0) + " step(s) · " + (data.model || "") + stopped);
          input.focus();
        })
        .catch(function (error) {
          window.clearTimeout(stillWorking);
          setBusy(false, "Error: " + error.message);
        });
    });

    if (input) {
      input.addEventListener("keydown", function (event) {
        if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
          event.preventDefault();
          form.requestSubmit();
        }
      });
    }
  }

  var draftKey = "jf_agent_draft";
  try {
    var draft = window.sessionStorage.getItem(draftKey);
    if (draft && input && !input.value) {
      input.value = draft;
      window.sessionStorage.removeItem(draftKey);
      input.focus();
    }
  } catch (error) {
    // sessionStorage can be disabled; the page still works.
  }

  document.querySelectorAll(".suggestion").forEach(function (button) {
    button.addEventListener("click", function () {
      var prompt = button.getAttribute("data-prompt") || "";
      if (input && form) {
        input.value = prompt;
        input.focus();
        return;
      }
      fetch("/api/agent/threads", { method: "POST" })
        .then(function (response) {
          if (!response.ok) throw new Error("Could not create a conversation");
          return response.json();
        })
        .then(function (data) {
          if (data.thread && data.thread.id) {
            try {
              window.sessionStorage.setItem(draftKey, prompt);
            } catch (error) {
              // ignore storage failure
            }
            window.location = "/agent?thread=" + encodeURIComponent(data.thread.id);
          }
        })
        .catch(function (error) {
          if (status) status.textContent = "Error: " + error.message;
        });
    });
  });

  if (messages) {
    messages.scrollTop = messages.scrollHeight;
  }
})();
