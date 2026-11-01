# JobForge installer

`npx jobforge` installs the JobForge MCP server and Agent Skill into one or more
local coding agents. It does not require a model provider: the host agent uses
its own model.

The installer is a thin terminal UI. All setup logic lives in the Python
JobForge package (`jobforge install ...`); the Node package only resolves the
runtime, collects choices, and calls the Python engine.

```bash
npx jobforge            # install or update the runtime, then install into agents
npx jobforge --update   # force a runtime update to this installer version
npx jobforge --no-update
```

Optional browser automation (Chromium for filling applications) is offered
separately and is never installed without consent.
