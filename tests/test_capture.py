import dataclasses
import unittest

from brain import capture
from brain.frontmatter import parse_file
from tests.helpers import TempVault


class TestCapture(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_capture_writes_valid_note_in_inbox(self):
        path = capture.capture(self.config, type_="fact", title="Test Fact", text="Some detail.")
        self.assertTrue(path.exists())
        self.assertEqual(path.parent.name, "00_INBOX")
        note = parse_file(path)
        self.assertEqual(note.type, "fact")
        self.assertEqual(note.id, "fact-test-fact")

    def test_capture_rejects_unknown_type(self):
        with self.assertRaises(ValueError):
            capture.capture(self.config, type_="bogus", title="X")

    def test_capture_avoids_filename_collision(self):
        p1 = capture.capture(self.config, type_="fact", title="Same Title")
        p2 = capture.capture(self.config, type_="fact", title="Same Title")
        self.assertNotEqual(p1, p2)
        self.assertTrue(p1.exists())
        self.assertTrue(p2.exists())

    def test_slugify(self):
        self.assertEqual(capture.slugify("Hello, World!"), "hello-world")
        self.assertEqual(capture.slugify("  spaced out  "), "spaced-out")


class TestCaptureSetsOwnership(unittest.TestCase):
    """Stage 2 (multi-user visibility): a new record defaults to private
    ownership of the authenticated principal unless a permitted explicit
    audience is supplied — never inferred, never left unset."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_owner_principal_is_the_acting_principal(self):
        config = dataclasses.replace(self.config, acting_principal="principal-ania")
        dest = capture.capture(config, type_="fact", title="A fact")
        note = parse_file(dest)
        self.assertEqual(note.meta["owner_principal"], "principal:ania")

    def test_audience_defaults_to_private(self):
        dest = capture.capture(self.config, type_="fact", title="A fact")
        note = parse_file(dest)
        self.assertEqual(note.meta["audience"], [])

    def test_explicit_audience_is_honored(self):
        dest = capture.capture(self.config, type_="fact", title="A fact",
                                audience=["group:household"])
        note = parse_file(dest)
        self.assertEqual(note.meta["audience"], ["group:household"])


if __name__ == "__main__":
    unittest.main()
