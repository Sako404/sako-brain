import unittest
from pathlib import Path

from brain import frontmatter


class TestFrontmatter(unittest.TestCase):
    def test_parse_valid_note(self):
        text = (
            "---\n"
            "id: fact-sky-is-blue\n"
            "type: fact\n"
            "status: current\n"
            "tags: [nature, color]\n"
            "---\n"
            "\n"
            "# The sky is blue\n"
            "\n"
            "Body text here.\n"
        )
        note = frontmatter.parse_text(text, Path("dummy.md"))
        self.assertEqual(note.id, "fact-sky-is-blue")
        self.assertEqual(note.type, "fact")
        self.assertEqual(note.title, "The sky is blue")
        self.assertEqual(note.list_field("tags"), ["nature", "color"])

    def test_missing_frontmatter_raises(self):
        with self.assertRaises(frontmatter.FrontmatterError):
            frontmatter.parse_text("# No frontmatter here\n", Path("dummy.md"))

    def test_unterminated_frontmatter_raises(self):
        with self.assertRaises(frontmatter.FrontmatterError):
            frontmatter.parse_text("---\nid: x\n", Path("dummy.md"))

    def test_invalid_yaml_raises(self):
        text = "---\nid: [unterminated\n---\nbody\n"
        with self.assertRaises(frontmatter.FrontmatterError):
            frontmatter.parse_text(text, Path("dummy.md"))

    def test_render_round_trip(self):
        text = "---\nid: fact-x\ntype: fact\n---\n\n# X\n\nbody\n"
        note = frontmatter.parse_text(text, Path("dummy.md"))
        rendered = frontmatter.render(note)
        reparsed = frontmatter.parse_text(rendered, Path("dummy.md"))
        self.assertEqual(reparsed.id, "fact-x")
        self.assertIn("# X", reparsed.body)


if __name__ == "__main__":
    unittest.main()
