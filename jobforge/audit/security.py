"""Static security audit for the local app. Not a substitute for a pentest."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
SCAN_DIRS = [ROOT / "jobforge"]
SKIP_PARTS = {"__pycache__", ".venv", "node_modules", ".git"}
SECRET_PATTERNS = [
    (r"\bsk-[A-Za-z0-9]{20,}\b", "possible OpenAI-style API key"),
    (r"\bAKIA[0-9A-Z]{16}\b", "possible AWS access key"),
    (r"\bghp_[A-Za-z0-9]{20,}\b", "possible GitHub token"),
    (r"(?i)(api[_-]?key|secret|password)\s*=\s*[\"'][^\"']{8,}[\"']", "hardcoded secret-looking assignment"),
]
DANGER_PATTERNS = [
    (r"\beval\s*\(", "eval()"),
    (r"\bexec\s*\(", "exec()"),
    (r"pickle\.loads\s*\(", "pickle.loads()"),
    (r"subprocess\.[A-Za-z_]+\([^)]*shell\s*=\s*True", "subprocess with shell=True"),
    (r"os\.system\s*\(", "os.system()"),
    (r"requests\.[a-z]+\([^)]*verify\s*=\s*False", "TLS verification disabled"),
    (r"\|\s*safe\b", "Jinja |safe on user data"),
    (r"\ballow_submit\b[\"']?\s*\]?\s*[:=]\s*True", "auto-submit enabled"),
    (
        r"capsolver|2captcha|anticaptcha|\bsolve_captcha\s*\(|\bcaptcha\.solve\s*\(|"
        r"\bcaptcha_solver\b[\"']?\s*[:=]\s*true\b|Base64.*captcha",
        "captcha-solving reference",
    ),
    (r"stealth|webdriver.*false|navigator\.webdriver", "anti-bot evasion reference"),
]

# A reviewed, risk-gated call site can opt out of one named finding with this marker.
AUDIT_ALLOW = re.compile(
    r"#\s*security-audit:\s*allow\s+([A-Za-z0-9 _-]+)", re.IGNORECASE
)


def find_danger(text: str) -> list[tuple[int, str]]:
    """Return (line, label) for each unmarked dangerous pattern in text."""
    findings: list[tuple[int, str]] = []
    lines = text.splitlines()
    for pattern, label in DANGER_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            line_number = text[: match.start()].count("\n") + 1
            line_start = text.rfind("\n", 0, match.start()) + 1
            match_end_col = match.end() - line_start
            source_line = lines[line_number - 1] if 0 < line_number <= len(lines) else ""
            marker = AUDIT_ALLOW.search(source_line)
            # Suppression is finding-specific and must follow the match. A marker
            # inside a string literal before the match does not suppress it.
            if (
                marker
                and marker.start() >= match_end_col
                and marker.group(1).strip().lower() == label.lower()
            ):
                continue
            findings.append((line_number, label))
    return findings


def _iter_files() -> list[Path]:
    files: list[Path] = []
    for directory in SCAN_DIRS:
        for path in directory.rglob("*"):
            if path.is_file() and not any(part in SKIP_PARTS for part in path.parts):
                if "audit" in path.parts:
                    continue
                if path.suffix in {".py", ".html", ".js", ".css", ".yml", ".yaml", ".json", ".md"}:
                    files.append(path)
    return files


def run_security() -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    for path in _iter_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pattern, label in SECRET_PATTERNS:
            for match in re.finditer(pattern, text):
                findings.append(
                    {
                        "severity": "high",
                        "kind": label,
                        "file": str(path.relative_to(ROOT)),
                        "line": text[: match.start()].count("\n") + 1,
                    }
                )
        for line, label in find_danger(text):
            findings.append(
                {
                    "severity": "medium",
                    "kind": label,
                    "file": str(path.relative_to(ROOT)),
                    "line": line,
                }
            )

    # Deduplicate by file/kind/line.
    unique = {(f["file"], f["kind"], f["line"]): f for f in findings}
    findings = sorted(unique.values(), key=lambda f: (f["severity"], f["file"], f["line"]))
    return {
        "files_scanned": len(_iter_files()),
        "findings": findings,
        "high": sum(1 for f in findings if f["severity"] == "high"),
        "medium": sum(1 for f in findings if f["severity"] == "medium"),
        "note": "Static heuristics only. Review every hit; examples in docs/config may be false positives.",
    }
