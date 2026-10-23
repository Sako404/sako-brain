"""brain.commandpolicy against the real CLI and the shipped skills.

The client, the dispatcher and the skills disagreed three times before this
module existed (`index` in v0.11.1, `memory` in v0.15.0, plus a stale
production dispatcher). These tests walk `cli.build_parser()` and the skill
files directly, so a new or renamed command fails the run it lands in.
"""
from __future__ import annotations

import unittest

from brain import commandpolicy as cp
from brain import remote

from tests.test_skills_cli_contract import SKILLS_DIR, _leaf_parsers, _referenced_commands

CATEGORIES = {
    "READ": cp.READ,
    "WRITE": cp.WRITE,
    "LOCAL_ONLY": cp.LOCAL_ONLY,
    "HOST_ONLY": cp.HOST_ONLY,
}


def _top_level_commands() -> set[str]:
    return {leaf.split()[0] for leaf in _leaf_parsers()}


class TestEveryCommandIsClassified(unittest.TestCase):
    def test_every_top_level_command_is_in_exactly_one_category(self):
        problems = []
        for command in sorted(_top_level_commands()):
            homes = [name for name, members in CATEGORIES.items() if command in members]
            if len(homes) != 1:
                problems.append(f"{command}: {homes or 'unclassified'}")
        self.assertEqual(problems, [], "classify each in brain/commandpolicy.py:\n" + "\n".join(problems))

    def test_no_category_names_a_command_the_cli_does_not_have(self):
        known = _top_level_commands()
        for name, members in CATEGORIES.items():
            with self.subTest(category=name):
                self.assertEqual(set(members) - known, set())

    def test_pairs_name_real_subcommands(self):
        leaves = set(_leaf_parsers())
        for pair in cp.READ_ONLY_PAIRS | cp.WRITE_ONLY_PAIRS:
            with self.subTest(pair=pair):
                self.assertIn(" ".join(pair), leaves)

    def test_pairs_refine_the_right_coarse_sets(self):
        for top, _ in cp.READ_ONLY_PAIRS:
            self.assertIn(top, cp.WRITE)
        for top, _ in cp.WRITE_ONLY_PAIRS:
            self.assertIn(top, cp.READ)


class TestModesStayInsideTheirBoundary(unittest.TestCase):
    def test_no_mode_grants_a_non_dispatchable_command(self):
        never = cp.LOCAL_ONLY | cp.HOST_ONLY
        for mode, allowed in cp.MODES.items():
            with self.subTest(mode=mode):
                self.assertEqual(allowed & never, set())

    def test_narrow_modes_are_subsets_of_write(self):
        for mode in ("read", "capture", "status"):
            with self.subTest(mode=mode):
                self.assertLessEqual(cp.MODES[mode], cp.MODES["write"])

    def test_read_mode_grants_no_write_top_level(self):
        self.assertEqual(cp.MODES["read"] & cp.WRITE, set())


class TestClientAndDispatcherAgree(unittest.TestCase):
    """The key the client presents must be one the dispatcher accepts for
    every dispatchable leaf command."""

    def test_every_dispatchable_leaf_passes_under_the_identity_the_client_picks(self):
        problems = []
        for leaf in sorted(_leaf_parsers()):
            argv = leaf.split()
            if argv[0] not in cp.READ | cp.WRITE:
                continue
            mode = "write" if remote._is_write_subcommand(argv) else "read"
            allowed, reason = cp.is_allowed(mode, argv)
            if not allowed:
                problems.append(f"brain {leaf}: client picks {mode}, dispatcher says {reason}")
        self.assertEqual(problems, [], "\n".join(problems))


class TestShippedSkillsWorkAgainstARemoteBrain(unittest.TestCase):
    """A skill that tells an agent to run a command the dispatcher refuses
    fails silently for every user of a server-hosted Brain."""

    def test_every_skill_command_is_dispatchable_or_documents_its_remote_fallback(self):
        problems = []
        for skill_file in sorted(SKILLS_DIR.glob("*/SKILL.md")):
            text = skill_file.read_text(encoding="utf-8")
            for full, _ in _referenced_commands(text):
                argv = full.split()
                top = argv[0]
                if top in cp.LOCAL_ONLY:
                    continue
                if not cp.is_allowed("write", argv)[0]:
                    problems.append(f"{skill_file.parent.name}: 'brain {full}' is never dispatchable")
        self.assertEqual(problems, [], "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
