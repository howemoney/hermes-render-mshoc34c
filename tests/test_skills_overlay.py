"""Shape checks for the skills overlay in ./skills (-> /opt/render-tools/skills-local).

Why these tests exist:

- Hermes resolves a skill by its frontmatter ``name``; an overlay skill whose
  ``name`` differs from its directory name is reachable by one spelling in
  ``skills_list`` and another in ``skill_view``, which is exactly the kind of
  ambiguity that made the dispatcher's force-load of ``sdlc-review`` break
  when an overlay reused that name. So: ``name == directory``.
- The overlay dir sits on ``skills.external_dirs`` next to upstream's bundled
  ``skills/`` tree. A same-named skill shadows upstream (or vice versa,
  depending on precedence) and the dispatcher force-loads some upstream names
  (``sdlc-review`` on every review spawn) — so overlay names must never
  collide with an upstream skill directory name.
- ``description`` is what the model sees in the skills index; a trailing
  period is the house convention for a one-sentence trigger-first line.

The frontmatter parse uses PyYAML when it is installed (it is, inside the
Hermes image, where ``python -m unittest discover -s /tests`` runs) and a
minimal key: value scanner otherwise, so the structural checks still run on
a bare Mac. Only the "full YAML document parses" assertion is skipped
without PyYAML.

The upstream collision check reads the checked-out Hermes source named by
``HERMES_UPSTREAM_SRC`` (default: the scratchpad checkout of the deployed
tag). If that path is missing the collision test is skipped, never failed —
the build must not depend on a developer-machine path.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO_ROOT / "skills"

# Deployed-to-be tag checkout used for verification while authoring PR-B.
DEFAULT_UPSTREAM_SRC = (
    "/private/tmp/claude-501/-Users-howemoney26-stopsargassum/"
    "1c4f5ecb-e883-4187-915d-45ece1384f08/scratchpad/hermes-v818"
)

try:  # pragma: no cover - exercised differently depending on the host
    import yaml  # type: ignore
except Exception:  # noqa: BLE001 - any import failure means "no PyYAML here"
    yaml = None  # type: ignore[assignment]


def split_frontmatter(text: str) -> str:
    """Return the raw YAML between the leading '---' fence lines.

    Mirrors the simple split Hermes performs: the file must START with a
    '---' line and the block ends at the next '---' line. Raises
    AssertionError with a useful message otherwise.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise AssertionError("SKILL.md must start with a '---' frontmatter fence")
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            return "\n".join(lines[1:idx])
    raise AssertionError("frontmatter fence '---' is never closed")


def parse_scalars(block: str) -> dict[str, str]:
    """Minimal fallback parser: top-level ``key: value`` scalars only.

    Good enough for ``name`` and ``description``; nested mappings
    (``metadata:``) and lists are ignored on purpose.
    """
    out: dict[str, str] = {}
    for line in block.splitlines():
        if not line or line[0] in " \t#" or ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def load_frontmatter(path: Path) -> dict:
    block = split_frontmatter(path.read_text(encoding="utf-8"))
    if yaml is not None:
        data = yaml.safe_load(block)
        if not isinstance(data, dict):
            raise AssertionError(f"{path}: frontmatter is not a mapping")
        return data
    return parse_scalars(block)


def overlay_skill_files() -> list[Path]:
    return sorted(SKILLS_DIR.glob("*/SKILL.md"))


def upstream_skill_names() -> set[str] | None:
    root = Path(os.environ.get("HERMES_UPSTREAM_SRC", DEFAULT_UPSTREAM_SRC))
    skills_root = root / "skills"
    if not skills_root.is_dir():
        return None
    return {p.parent.name for p in skills_root.rglob("SKILL.md")}


class SkillsOverlayTests(unittest.TestCase):
    def test_overlay_has_the_sdlc_skills(self):
        names = {p.parent.name for p in overlay_skill_files()}
        self.assertIn("kanban-sdlc-worker", names)
        self.assertIn("kanban-sdlc-reviewer", names)
        self.assertIn("render-on-hermes", names)

    def test_frontmatter_splits(self):
        for path in overlay_skill_files():
            with self.subTest(skill=path.parent.name):
                block = split_frontmatter(path.read_text(encoding="utf-8"))
                self.assertTrue(block.strip(), "empty frontmatter")

    def test_frontmatter_is_valid_yaml_mapping(self):
        if yaml is None:
            self.skipTest("PyYAML not installed; full-parse check skipped")
        for path in overlay_skill_files():
            with self.subTest(skill=path.parent.name):
                data = yaml.safe_load(split_frontmatter(path.read_text(encoding="utf-8")))
                self.assertIsInstance(data, dict)
                self.assertIsInstance(data.get("name"), str)
                self.assertIsInstance(data.get("description"), str)

    def test_name_matches_directory(self):
        for path in overlay_skill_files():
            with self.subTest(skill=path.parent.name):
                data = load_frontmatter(path)
                self.assertEqual(data.get("name"), path.parent.name)

    def test_description_is_one_trigger_sentence_ending_with_period(self):
        for path in overlay_skill_files():
            with self.subTest(skill=path.parent.name):
                desc = str(load_frontmatter(path).get("description", "")).strip()
                self.assertTrue(desc, "missing description")
                self.assertTrue(desc.endswith("."), f"description must end with '.': {desc[-40:]!r}")
                self.assertNotIn("\n", desc, "description must be a single line")

    def test_sdlc_skills_declare_kanban_environment_and_toolsets(self):
        # Matches upstream skills/devops/sdlc-review/SKILL.md: ``environments``
        # is a TOP-LEVEL key (agent/skill_utils.py:skill_matches_environment)
        # and ``requires_toolsets`` lives under metadata.hermes.
        if yaml is None:
            self.skipTest("PyYAML not installed; nested-frontmatter check skipped")
        for name in ("kanban-sdlc-worker", "kanban-sdlc-reviewer"):
            with self.subTest(skill=name):
                data = load_frontmatter(SKILLS_DIR / name / "SKILL.md")
                self.assertIn("kanban", data.get("environments", []))
                hermes = data.get("metadata", {}).get("hermes", {})
                self.assertEqual(
                    hermes.get("requires_toolsets"), ["kanban", "terminal", "file"]
                )

    def test_worker_references_exist_and_are_pointed_at(self):
        worker = SKILLS_DIR / "kanban-sdlc-worker"
        text = (worker / "SKILL.md").read_text(encoding="utf-8")
        for ref in ("references/gate.md", "references/pr-template.md"):
            with self.subTest(ref=ref):
                self.assertTrue((worker / ref).is_file(), f"missing {ref}")
                self.assertIn(ref, text, f"SKILL.md never tells the worker to skill_view {ref}")
        # The reviewer runs the same GATE — it must point at the worker's copy,
        # not carry a second (drifting) one.
        reviewer = (SKILLS_DIR / "kanban-sdlc-reviewer" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn('skill_view(name="kanban-sdlc-worker", file_path="references/gate.md")', reviewer)

    def test_worker_skill_stays_compact(self):
        # The full gate script and PR template live in references/; SKILL.md
        # is the protocol only. Upper bound is deliberately loose.
        lines = (SKILLS_DIR / "kanban-sdlc-worker" / "SKILL.md").read_text(encoding="utf-8").splitlines()
        self.assertLessEqual(len(lines), 400, f"worker SKILL.md is {len(lines)} lines; move detail to references/")

    def test_terminal_action_contract_and_pr_url_rule(self):
        worker = (SKILLS_DIR / "kanban-sdlc-worker" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("TERMINAL-ACTION CONTRACT", worker)
        self.assertIn('kanban_request_review', worker)
        self.assertIn('reviewer="reviewer"', worker)
        self.assertIn("NEVER put the PR URL in a `kanban_comment`", worker)
        for kind in ("needs_input", "capability", "transient", "dependency"):
            self.assertIn(f"`{kind}`", worker)
        reviewer = (SKILLS_DIR / "kanban-sdlc-reviewer" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("--match-head-commit", reviewer)
        self.assertIn('skills=["merge-reconciler"]', reviewer)
        self.assertIn("kanban_link(parent_id=", reviewer)

    def test_no_collision_with_upstream_skill_names(self):
        upstream = upstream_skill_names()
        if upstream is None:
            self.skipTest("upstream Hermes source not available (set HERMES_UPSTREAM_SRC)")
        # Sanity: the checkout really is a Hermes tree with the skills we build on.
        self.assertIn("sdlc-review", upstream)
        self.assertIn("merge-reconciler", upstream)
        for path in overlay_skill_files():
            with self.subTest(skill=path.parent.name):
                self.assertNotIn(
                    path.parent.name,
                    upstream,
                    f"overlay skill {path.parent.name!r} collides with an upstream skill directory",
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
