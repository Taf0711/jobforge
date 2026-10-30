# Kobra AI-editor component adaptation

Source inspected: <https://kobra.systems/components/ai-editor> (public component
code). JobForge uses a server-rendered Jinja + vanilla-JS UI with no npm, React,
or build step, so the useful interaction patterns were reimplemented locally
rather than copying the React component or its assets.

## Adopted

| Kobra pattern | JobForge implementation |
|---|---|
| Selection toolbar with **Ask AI** | `static/selection-ask.js` — select text on a job description or agent answer; a floating "Ask about this…" popover appears. It fills the agent composer, or creates a thread and carries the selection as a draft. |
| Context-aware selection | `data-ask-surface` + `data-ask-context` attributes on the job description and agent messages. The draft includes the source surface and the selected text. |
| Streaming answer reveal | `static/agent.js` reveals the final assistant answer in small word chunks with a caret. Reduced-motion users get the full answer immediately. |
| Orb + shimmer status | CSS-only `.agent-orb` and `.agent-shimmer` while a run is active; the status rotates to "Still working" after 8 seconds. |
| Tool timeline | `static/agent.js` renders the run's `tool_trace` as a collapsible timeline with summaries and a "write blocked" badge. |
| Reduced-motion support | `@media (prefers-reduced-motion: reduce)` disables orb/shimmer/caret animations. |
| Keyboard affordance | The selection popover supports Enter to submit and Escape to dismiss. |

## Deliberately not adopted

| Kobra pattern | Why |
|---|---|
| React, Motion, Base UI, Tabler icons | JobForge has no npm/node/build step and must stay server-rendered. |
| Content-editable rich-text editor with bold/italic/underline/code marks | Resume output is Typst-rendered; answer-bank fields are plain text. A contentEditable surface would add HTML-injection and selection-restoration risk with no user value. |
| Google favicon "sources" strip | Violates local-first/no-telemetry expectations. JobForge would use local fact IDs or no source strip. |
| Word-by-word true streaming | The provider client is non-streaming today; the UI reveal is honest post-response animation, not a fake stream. Real SSE is a separate backend change. |
| RTL direction provider | The current UI is English/LTR; can be added when the interface is localized. |

## Files

- `jobforge/web/static/selection-ask.js`
- `jobforge/web/static/agent.js`
- `jobforge/web/static/app.css`
- `jobforge/web/templates/base.html`
- `jobforge/web/templates/job_detail.html`
- `jobforge/web/templates/agent.html`

## Verification

- Select text on a job description → Ask → confirm the draft contains the
  selected text and source context.
- Select text on an agent answer → Ask → confirm the composer is filled, not a
  second thread.
- Send an agent message with reduced motion enabled → answer appears immediately.
- Run with a tool-using prompt → tool timeline appears after the response.
