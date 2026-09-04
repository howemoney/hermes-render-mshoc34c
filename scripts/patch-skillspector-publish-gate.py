#!/usr/bin/env python3
"""Make NVIDIA SkillSpector a fail-closed Hermes skill publish gate.

Build-time patch for root-owned ``hermes_cli/skills_hub.py``. The patch is
idempotent and fails loudly when the pinned upstream anchor changes.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

MARKER = "_HOWE_SKILLSPECTOR_PUBLISH_GATE"
ANCHOR = '''    # Self-scan before publishing
    c.print(f"[bold]Scanning '{name}' before publish...[/]")
    result = scan_skill(path, source="self")
    c.print(format_scan_report(result))
    if result.verdict == "dangerous":
        c.print("[bold red]Cannot publish a skill with DANGEROUS verdict.[/]\\n")
        return
'''
REPLACEMENT = '''    # _HOWE_SKILLSPECTOR_PUBLISH_GATE
    # NVIDIA SkillSpector is the mandatory, fail-closed publication gate. The
    # exact local directory is scanned by the pinned MCP-capable installation;
    # a scan error, incomplete scan, or unsafe verdict stops publication.
    import hashlib
    import json
    import subprocess

    c.print(f"[bold]NVIDIA SkillSpector gate: scanning '{name}'...[/]")
    skill_hash = hashlib.sha256()
    for candidate in sorted(p for p in path.rglob("*") if p.is_file()):
        skill_hash.update(str(candidate.relative_to(path)).encode("utf-8"))
        skill_hash.update(b"\\0")
        skill_hash.update(candidate.read_bytes())
        skill_hash.update(b"\\0")
    artifact_sha256 = skill_hash.hexdigest()
    command = [
        "/opt/skillspector/bin/skillspector", "scan", str(path),
        "--no-llm", "--fail-on-incomplete", "--format", "json",
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=300, shell=False,
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": str(Path.home()),
                "LANG": "C.UTF-8",
                "LANGCHAIN_TRACING_V2": "false",
            },
        )
        payload = json.loads(completed.stdout)
        assessment = payload.get("risk_assessment") or {}
        score = assessment.get("score")
        recommendation = assessment.get("recommendation")
        completeness = payload.get("metadata", {}).get("analysis_completeness", {})
        if not isinstance(score, int) or not recommendation:
            raise ValueError("scan returned no usable score/recommendation")
        c.print(
            f"[bold]SkillSpector score:[/] {score}/100 · {recommendation} "
            f"· sha256 {artifact_sha256[:16]}…"
        )
        if completed.returncode != 0 or recommendation != "SAFE":
            c.print(
                "[bold red]Publication blocked by NVIDIA SkillSpector "
                "(Howe policy requires SAFE; CAUTION needs human remediation/re-scan).[/]\\n"
            )
            return
        if completeness and not completeness.get("is_complete", True):
            c.print("[bold red]Publication blocked: SkillSpector scan incomplete.[/]\\n")
            return
    except Exception as exc:
        c.print(f"[bold red]Publication blocked: SkillSpector gate failed: {exc}[/]\\n")
        return

    # Retain Hermes' native guard as independent defense-in-depth.
    c.print(f"[bold]Running Hermes native scan for '{name}'...[/]")
    result = scan_skill(path, source="self")
    c.print(format_scan_report(result))
    if result.verdict == "dangerous":
        c.print("[bold red]Cannot publish a skill with DANGEROUS verdict.[/]\\n")
        return
'''


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch-skillspector-publish-gate.py <skills_hub.py>")
    path = Path(sys.argv[1])
    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        print("skillspector publish gate already applied — no-op")
        return 0
    if text.count(ANCHOR) != 1:
        raise SystemExit("skillspector publish gate anchor missing or ambiguous")
    updated = text.replace(ANCHOR, REPLACEMENT)
    ast.parse(updated)
    path.write_text(updated, encoding="utf-8")
    print("skillspector publish gate applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
