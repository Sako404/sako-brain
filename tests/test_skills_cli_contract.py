"""Skills <-> CLI contract (v0.11.0 / item 3 of the Public Agent Integration
work): the shipped Claude Code skills are part of the product, and every
`brain <command>` (and, where cheaply checkable, `--flag`) they reference
must be a real, current command this package's own CLI accepts.

Authoritative by construction: every check here walks `cli.build_parser()`
directly (the same parser `brain` itself dispatches through) rather than a
hand-kept list or a regex against source — a renamed/removed subcommand or
flag fails this test the same run it lands, not months later when a user's
skill silently stops working.

Also guards the generalization work itself: a skill must never regress to
naming a private deployment detail (a concrete host/IP, a specific user's
home directory, a hardcoded vault filesystem path) or route around
canonical Brain (`BRAIN_LOCAL`, editing vault files directly).
"""
from __future__ import annotations

import argparse
import re
import unittest
from pathlib import Path

from brain import cli
from brain.integrations_cli import shipped_skill_names, _integrations_root

SKILLS_DIR = Path(str(_integrations_root() / "claude-code" / "skills"))

# `brain <word> <word>?` inside a backtick span — captures at most the first
# two words after "brain".
COMMAND_PATTERN = re.compile(r"`brain ([a-z][a-z-]*)(?: ([a-z][a-z-]*))?[^`]*`")
FLAG_PATTERN = re.compile(r"(--[a-z][a-z-]*)")

# Bare mentions of a command family in prose ("the `brain project` family")
# are not a claim that the bare form is itself runnable.
NON_LEAF_PARENTS = {"project", "decision", "timeline", "note", "handoff", "memory", "git", "backup"}

# Patterns a public, generic skill must never contain — remnants of a
# private deployment, or an instruction to route around canonical Brain.
# The home-directory pattern is assembled from parts (not spelled as one
# contiguous literal here) so this source file itself doesn't trip the
# project's own OSS-2 generic-path leak gate (test_oss2_neutralisation.py).
_HOME_DIR_PATTERN = re.compile("/" + "home/" + r"(?!user\b)[a-z0-9_-]+")
PROHIBITED_PATTERNS = [
    (_HOME_DIR_PATTERN, "a concrete home directory path for a specific user"),
    (re.compile(r"\bTrueNAS\b"), "a specific deployment's infrastructure name"),
    (re.compile(r"\bNextcloud\b"), "a specific deployment's infrastructure name"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "a literal IP address"),
    (re.compile(r"BRAIN_LOCAL"), "an instruction referencing the local-vault-forcing override"),
    (re.compile(r"\bediting? .*vault file", re.IGNORECASE), None),  # checked separately below, kept for discoverability
]


def _leaf_parsers() -> dict[str, argparse.ArgumentParser]:
    """{'project show': <subparser>, 'search': <subparser>, ...} for every
    leaf command/subcommand build_parser() actually accepts."""
    leaves: dict[str, argparse.ArgumentParser] = {}

    def walk(parser: argparse.ArgumentParser, prefix: str = ""):
        sub_action = next(
            (a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None
        )
        if not sub_action:
            if prefix:
                leaves[prefix.strip()] = parser
            return
        if not sub_action.required and prefix:
            leaves[prefix.strip()] = parser
        for name, subparser in sub_action.choices.items():
            walk(subparser, prefix=f"{prefix}{name} ")

    walk(cli.build_parser())
    return leaves


def _flag_strings(parser: argparse.ArgumentParser) -> set[str]:
    flags: set[str] = set()
    for action in parser._actions:
        flags.update(o for o in action.option_strings if o.startswith("--"))
    return flags


def _referenced_commands(text: str) -> set[tuple[str, str]]:
    found = set()
    for m in COMMAND_PATTERN.finditer(text):
        first, second = m.group(1), m.group(2)
        if second:
            full = f"{first} {second}"
        else:
            if first in NON_LEAF_PARENTS:
                continue
            full = first
        found.add((full, m.group(0)))
    return found


def _referenced_flags_by_command(text: str) -> dict[str, set[str]]:
    """For each backtick span containing a `brain <cmd>...` reference,
    the --flags mentioned in that same span, keyed by the (leaf) command."""
    result: dict[str, set[str]] = {}
    for m in re.finditer(r"`[^`]*`", text):
        span = m.group(0)
        cmd_m = COMMAND_PATTERN.match(span)
        if not cmd_m:
            continue
        first, second = cmd_m.group(1), cmd_m.group(2)
        if not second and first in NON_LEAF_PARENTS:
            continue
        full = f"{first} {second}" if second else first
        flags = set(FLAG_PATTERN.findall(span))
        if flags:
            result.setdefault(full, set()).update(flags)
    return result


class TestShippedSkillsMatchTheCLI(unittest.TestCase):
    """Every `brain <command>` a shipped skill references must exist."""

    @classmethod
    def setUpClass(cls):
        cls.leaves = _leaf_parsers()
        cls.skill_files = sorted(SKILLS_DIR.glob("*/SKILL.md"))

    def test_skills_directory_is_not_empty(self):
        self.assertGreaterEqual(len(self.skill_files), 15,
                                 "expected at least the 15 known workflows to be shipped")

    def test_every_referenced_command_exists(self):
        problems = []
        for skill_file in self.skill_files:
            text = skill_file.read_text(encoding="utf-8")
            for full, matched in _referenced_commands(text):
                if full not in self.leaves:
                    problems.append(f"{skill_file.name}: references 'brain {full}' ({matched}) "
                                     f"— not a real CLI command")
        self.assertEqual(problems, [], "skill/CLI command drift:\n" + "\n".join(problems))

    def test_every_referenced_flag_exists_on_its_command(self):
        problems = []
        for skill_file in self.skill_files:
            text = skill_file.read_text(encoding="utf-8")
            for full, flags in _referenced_flags_by_command(text).items():
                parser = self.leaves.get(full)
                if parser is None:
                    continue  # already reported by the command-existence test
                valid = _flag_strings(parser)
                for flag in flags:
                    if flag not in valid:
                        problems.append(f"{skill_file.name}: 'brain {full}' does not accept {flag} "
                                         f"(has: {sorted(valid)})")
        self.assertEqual(problems, [], "skill/CLI flag drift:\n" + "\n".join(problems))

    def test_shipped_skill_names_match_their_directories(self):
        self.assertEqual(
            shipped_skill_names(),
            sorted(p.parent.name for p in self.skill_files),
        )


class TestShippedSkillsAreGeneric(unittest.TestCase):
    """No shipped skill may name a private deployment detail or instruct
    routing around canonical Brain — see the module docstring."""

    @classmethod
    def setUpClass(cls):
        cls.skill_files = sorted(SKILLS_DIR.glob("*/SKILL.md"))

    def test_no_prohibited_private_or_bypass_patterns(self):
        problems = []
        for skill_file in self.skill_files:
            text = skill_file.read_text(encoding="utf-8")
            for pattern, label in PROHIBITED_PATTERNS:
                if label is None:
                    continue
                m = pattern.search(text)
                if m:
                    problems.append(f"{skill_file.name}: contains {label} ({m.group(0)!r})")
        self.assertEqual(problems, [], "private/deployment-specific content in a public skill:\n"
                          + "\n".join(problems))

    def test_no_direct_vault_filesystem_edit_instructions(self):
        """A skill may say 'never edit a vault file directly' (the correct
        policy statement) but must never instruct actually doing so."""
        forbidden = re.compile(
            r"\b(?:open|write to|edit)\b(?!\s+a vault file directly as a)[^.]*"
            r"\b\d{2}_[A-Z]+/",
        )
        problems = []
        for skill_file in self.skill_files:
            text = skill_file.read_text(encoding="utf-8")
            m = forbidden.search(text)
            if m:
                problems.append(f"{skill_file.name}: possible direct-vault-edit instruction: {m.group(0)!r}")
        self.assertEqual(problems, [], "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
