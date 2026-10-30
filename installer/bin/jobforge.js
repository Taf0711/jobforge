#!/usr/bin/env node
// Thin npx bootstrap for the JobForge MCP server + Agent Skill.
// All setup logic lives in the Python package (`jobforge install ...`).

import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

import {
  cancel,
  confirm,
  intro,
  isCancel,
  log,
  multiselect,
  note,
  outro,
  spinner,
  text,
} from "@clack/prompts";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const VERSION = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")).version;

function usage() {
  console.log(`JobForge installer ${VERSION}

Usage:
  npx jobforge

No arguments are required. The wizard detects your local agents, shows the
exact files it will write, and installs the MCP server and Agent Skill.

Environment:
  JOBFORGE_BIN       JobForge command to use (default: jobforge on PATH)
  JOBFORGE_PYTHON    Python executable to use with \`-m jobforge\`
  JOBFORGE_CONFIG    Path to config.yml
`);
}

function guard(value) {
  if (isCancel(value)) {
    cancel("Setup cancelled. No changes were made.");
    process.exit(0);
  }
  return value;
}

function commandExists(name) {
  if (!name || name.includes(path.sep)) return existsSync(name);
  const finder = process.platform === "win32" ? "where" : "which";
  const result = spawnSync(finder, [name], { stdio: "ignore" });
  return result.status === 0;
}

function isPythonJobForge(cmd, prefix) {
  const result = spawnSync(cmd, [...prefix, "--help"], {
    encoding: "utf8",
    timeout: 15_000,
  });
  const output = `${result.stdout || ""}${result.stderr || ""}`;
  return result.status === 0 && output.includes("Local-first job search workbench");
}

function repoRoot(start) {
  let current = path.resolve(start);
  for (let i = 0; i < 8; i += 1) {
    if (
      existsSync(path.join(current, "jobforge", "__init__.py")) &&
      existsSync(path.join(current, ".venv", "bin", "python"))
    ) {
      return current;
    }
    const parent = path.dirname(current);
    if (parent === current) break;
    current = parent;
  }
  return "";
}

function resolveJobForge() {
  const envBin = (process.env.JOBFORGE_BIN || "").trim();
  if (envBin && commandExists(envBin) && isPythonJobForge(envBin, [])) {
    return { cmd: envBin, prefix: [], config: process.env.JOBFORGE_CONFIG || "" };
  }

  if (commandExists("jobforge") && isPythonJobForge("jobforge", [])) {
    return {
      cmd: "jobforge",
      prefix: [],
      config: process.env.JOBFORGE_CONFIG || "",
    };
  }

  const localShim = path.join(process.env.HOME || "", ".local", "bin", "jobforge");
  if (existsSync(localShim) && isPythonJobForge(localShim, [])) {
    return {
      cmd: localShim,
      prefix: [],
      config: process.env.JOBFORGE_CONFIG || "",
    };
  }

  const envPython = (process.env.JOBFORGE_PYTHON || "").trim();
  if (envPython && isPythonJobForge(envPython, ["-m", "jobforge"])) {
    return {
      cmd: envPython,
      prefix: ["-m", "jobforge"],
      config: process.env.JOBFORGE_CONFIG || "",
    };
  }

  for (const python of ["python3", "python"]) {
    if (isPythonJobForge(python, ["-m", "jobforge"])) {
      return {
        cmd: python,
        prefix: ["-m", "jobforge"],
        config: process.env.JOBFORGE_CONFIG || "",
      };
    }
  }

  const root = repoRoot(process.cwd()) || repoRoot(path.resolve(__dirname, "../.."));
  if (root) {
    const python = path.join(root, ".venv", "bin", "python");
    const config = path.join(root, "config.yml");
    return {
      cmd: python,
      prefix: ["-m", "jobforge"],
      config: process.env.JOBFORGE_CONFIG || (existsSync(config) ? config : ""),
    };
  }

  return null;
}

function runRaw(command, args, { timeout = 120_000 } = {}) {
  return spawnSync(command, args, {
    encoding: "utf8",
    timeout,
    maxBuffer: 20 * 1024 * 1024,
  });
}

function runCommand(target, args, { timeout = 120_000 } = {}) {
  const command = [
    ...target.prefix,
    ...(target.config ? ["--config", target.config] : []),
    ...args,
  ];
  return spawnSync(target.cmd, command, {
    encoding: "utf8",
    timeout,
    maxBuffer: 20 * 1024 * 1024,
  });
}

function runJson(target, args, options = {}) {
  const result = runCommand(target, args, options);
  if (result.error) {
    throw new Error(result.error.message);
  }
  const stdout = (result.stdout || "").trim();
  const stderr = (result.stderr || "").trim();
  if (result.status !== 0) {
    throw new Error(stderr || stdout || `Command failed with status ${result.status}`);
  }
  try {
    return JSON.parse(stdout);
  } catch {
    throw new Error(`JobForge returned non-JSON output:\n${stdout.slice(0, 800)}`);
  }
}

async function ensureRuntime() {
  const existing = resolveJobForge();
  if (existing) return existing;

  log.warn("JobForge runtime not found.");
  if (!commandExists("uv")) {
    note(
      "JobForge is Python-based. Install uv first, then run `npx jobforge` again:\n\n" +
        "  macOS/Linux: curl -LsSf https://astral.sh/uv/install.sh | sh\n" +
        "  Windows:     powershell -c \"irm https://astral.sh/uv/install.ps1 | iex\"",
      "Runtime required",
    );
    return null;
  }

  const proceed = guard(
    await confirm({
      message: "Install the JobForge Python runtime with uv?",
      initialValue: true,
    }),
  );
  if (!proceed) return null;

  const spec =
    process.env.JOBFORGE_INSTALL_SPEC ||
    "git+https://github.com/Taf0711/jobforge@main";
  const result = runRaw(
    "uv",
    ["tool", "install", "--from", spec, "jobforge"],
    { timeout: 10 * 60_000 },
  );
  if (result.status !== 0) {
    log.error("The JobForge runtime install failed.");
    log.message((result.stderr || result.stdout || "").trim().slice(0, 1200));
    return null;
  }
  return resolveJobForge();
}

async function ensureTypst() {
  if (commandExists("typst")) return true;
  log.warn("Typst is not installed. It is required to render resumes.");
  let command = null;
  let args = [];
  if (process.platform === "darwin" && commandExists("brew")) {
    command = "brew";
    args = ["install", "typst"];
  } else if (process.platform === "win32" && commandExists("winget")) {
    command = "winget";
    args = ["install", "--id", "Typst.Typst", "-e"];
  }
  if (!command) {
    note(
      "Install Typst 0.14+ and re-run the wizard:\n" +
        "  macOS: brew install typst\n" +
        "  Windows: winget install Typst.Typst\n" +
        "  Linux: https://typst.app/docs/guides/installation/",
      "Typst required",
    );
    return false;
  }
  const proceed = guard(
    await confirm({
      message: `Install Typst with ${command}? This is a separate third-party package.`,
      initialValue: true,
    }),
  );
  if (!proceed) return false;
  const result = runRaw(command, args, { timeout: 10 * 60_000 });
  if (result.status !== 0) {
    log.error("Typst install failed.");
    log.message((result.stderr || result.stdout || "").trim().slice(0, 800));
    return false;
  }
  return commandExists("typst");
}

function expandHome(value) {
  if (value === "~") return process.env.HOME || value;
  if (value.startsWith("~/")) return path.join(process.env.HOME || "", value.slice(2));
  return value;
}

async function ensureProfile(target) {
  try {
    const status = runJson(target, ["status"]);
    if (status.profile) return status;
  } catch {
    // Fall through to first-run setup.
  }

  log.warn("No JobForge profile found.");
  const proceed = guard(
    await confirm({
      message: "Set up JobForge now with a resume?",
      initialValue: true,
    }),
  );
  if (!proceed) {
    outro("Run `jobforge setup` and import a resume, then run this wizard again.");
    process.exit(0);
  }

  const answer = guard(
    await text({
      message: "Path to your resume (PDF, DOCX, TXT, or MD):",
      placeholder: "~/Documents/resume.pdf",
      validate(value) {
        const resolved = expandHome(String(value || "").trim());
        if (!resolved) return "A resume path is required.";
        if (!existsSync(resolved)) return "File not found.";
        return undefined;
      },
    }),
  );
  const resume = expandHome(String(answer).trim());

  const setup = runCommand(target, ["setup"]);
  if (setup.status !== 0) {
    log.error("JobForge setup failed.");
    log.message((setup.stderr || setup.stdout || "").trim().slice(0, 1200));
    outro("Fix the setup error and re-run `npx jobforge`.");
    process.exit(1);
  }

  const imported = runCommand(target, ["import-profile", "--file", resume]);
  if (imported.status !== 0) {
    log.error("Could not import the resume.");
    log.message((imported.stderr || imported.stdout || "").trim().slice(0, 1200));
    outro("The install did not continue.");
    process.exit(1);
  }
  log.success("Resume imported and profile created.");
  return runJson(target, ["status"]);
}

function hostHint(host) {
  if (host.config_entry_present) return "JobForge already installed";
  if (host.detected) return "detected";
  if (host.manual) return "manual configuration";
  return "not detected";
}

async function main() {
  if (process.argv.includes("--help") || process.argv.includes("-h")) {
    usage();
    return;
  }
  if (process.argv.includes("--version") || process.argv.includes("-v")) {
    console.log(VERSION);
    return;
  }
  if (process.argv.includes("--check-runtime")) {
    console.log(JSON.stringify(resolveJobForge(), null, 2));
    return;
  }

  intro(`JobForge ${VERSION}`);
  const target = await ensureRuntime();
  if (!target) {
    log.error("JobForge is not installed or its Python runtime is not available.");
    note(
      "Install the Python package first, or point the wizard at it:\n\n" +
        "  JOBFORGE_PYTHON=/path/to/python npx jobforge\n\n" +
        "From a source checkout, run `make bootstrap` first.",
      "Runtime not found",
    );
    outro("Nothing was changed.");
    process.exitCode = 1;
    return;
  }

  if (!(await ensureTypst())) {
    outro("Typst is required for resume rendering. Nothing was changed.");
    process.exitCode = 1;
    return;
  }

  let status;
  try {
    status = await ensureProfile(target);
  } catch (error) {
    log.error(`Could not read or create the JobForge profile: ${error.message}`);
    outro("Nothing was changed.");
    process.exitCode = 1;
    return;
  }

  note(`Profile: ${status.profile}\nJobs: ${status.jobs ?? 0}`, "JobForge");

  const hostPayload = runJson(target, ["install", "hosts"]);
  const hosts = hostPayload.hosts || [];
  const options = hosts.map((host) => ({
    value: host.id,
    label: host.label,
    hint: hostHint(host),
  }));
  const detected = hosts
    .filter((host) => host.detected && !host.manual)
    .map((host) => host.id);
  const selected = guard(
    await multiselect({
      message: "Install JobForge into which agents?",
      options,
      initialValues: detected,
      required: true,
    }),
  );
  if (!selected.length) {
    cancel("Select at least one agent.");
    process.exitCode = 1;
    return;
  }

  const selectedHosts = selected.join(",");
  let plan;
  try {
    plan = runJson(target, ["install", "plan", "--hosts", selectedHosts]);
  } catch (error) {
    log.error(`Could not build the install plan: ${error.message}`);
    outro("Nothing was changed.");
    process.exitCode = 1;
    return;
  }

  const lines = [];
  lines.push(`Profile: ${plan.profile.name || plan.profile.label} (id ${plan.profile.id})`);
  for (const host of plan.hosts) {
    lines.push("");
    lines.push(`${host.label}`);
    lines.push(`  method:  ${host.install_method}`);
    lines.push(`  config:  ${host.config_path}`);
    lines.push(`  skill:   ${host.skill_dir}`);
    lines.push(`  connection: ${host.connection}`);
    if (host.backup) lines.push(`  backup:  ${host.backup}`);
  }
  lines.push("");
  lines.push(`Scopes: ${plan.scopes.join(", ")}`);
  note(lines.join("\n"), "Review the plan");

  if (plan.warnings?.length) {
    for (const warning of plan.warnings) log.warn(warning);
  }

  const browser = (plan.optional || []).find((item) => item.id === "browser");
  let installBrowser = false;
  if (browser && !browser.installed) {
    installBrowser = guard(
      await confirm({
        message:
          "Install Chromium for filling applications? Optional, roughly 150 MB.",
        initialValue: false,
      }),
    );
  } else if (browser?.installed) {
    log.info("Browser engine already available; skipping the optional download.");
  }

  const proceed = guard(
    await confirm({
      message: "Apply this plan?",
      initialValue: true,
    }),
  );
  if (!proceed) {
    outro("No changes were made.");
    return;
  }

  const progress = spinner();
  progress.start("Installing JobForge...");
  let result;
  try {
    result = runJson(
      target,
      [
        "install",
        "apply",
        "--hosts",
        selectedHosts,
        "--yes",
        ...(installBrowser ? ["--install-browser"] : []),
      ],
      { timeout: 10 * 60_000 },
    );
  } catch (error) {
    progress.stop("Install failed");
    log.error(error.message);
    outro("The install did not complete. Re-run `npx jobforge` to inspect the plan.");
    process.exitCode = 1;
    return;
  }
  progress.stop(result.ok ? "Installed" : "Finished with errors");

  for (const host of result.hosts || []) {
    if (host.ok) {
      log.success(`${host.label}: ${host.method} · connection ${host.connection}`);
    } else {
      log.error(`${host.label}: ${host.error?.message || "failed"}`);
    }
  }
  for (const item of result.optional || []) {
    if (item.ok) log.success("Optional browser engine ready.");
    else log.warn(`Optional browser engine: ${item.error?.message || "not installed"}`);
  }

  try {
    const report = runJson(target, ["install", "doctor", "--hosts", selectedHosts]);
    if (report.ok) {
      log.success("Doctor: selected agents, connections, and skills look good.");
    } else {
      log.warn("Doctor found issues:");
      for (const issue of report.issues || []) log.warn(issue);
    }
  } catch (error) {
    log.warn(`Doctor check could not run: ${error.message}`);
  }

  note(
    "Restart the selected agent if its JobForge tools are not visible.\n" +
      'Then ask: "Use JobForge to find my best matches."',
    "Next",
  );
  outro("JobForge is installed. No model provider was configured.");
}

main().catch((error) => {
  log.error(error?.message || String(error));
  process.exitCode = 1;
});
