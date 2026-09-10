"""Sako Brain — local-first personal knowledge system tooling.

Markdown + YAML frontmatter under the Brain vault is the single source of
truth. Everything in this package is a rebuildable client of that Markdown:
the SQLite FTS5 index can always be thrown away and regenerated with
`brain index`.
"""

# Canonical version, and the only place it is written. Packaging reads it
# (`[tool.setuptools.dynamic] version = {attr = "brain.__version__"}`), the CLI
# prints it, and the MCP server reports it.
#
# OSS-4 reset this from an internal 1.0.0 to 0.1.0: public packaging and
# first-run are the beginning of the externally consumable lifecycle, and
# pre-1.0 states honestly that the CLI and configuration contract are not yet
# frozen. The internal 1.0.0 was real and is not being written out of history.
__version__ = "0.2.0"
