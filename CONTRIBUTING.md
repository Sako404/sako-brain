# Contributing to Sako Brain

Contributions are welcome. This is a small project maintained by one person, so
review may be slow — please open an issue before starting anything large, so we
can agree it is wanted before you spend an evening on it.

## Setup

```sh
git clone <this repository>
cd sako-brain
python -m venv .venv
.venv/bin/pip install -e .
```

Python **3.11 or newer** is required. The only runtime dependency is PyYAML;
there are no development dependencies to install.

## Tests

```sh
python -m unittest discover -s tests -t .
```

The suite is standard-library `unittest`. Tests for optional external tools
(`git`, `restic`, `systemd`) skip when the tool is absent, so a minimal machine
should still see a green run — if it does not, that is a bug worth reporting.

There are also packaging tests. They build a wheel, install it into a clean
virtualenv and run the CLI from outside the checkout, so they need network
access for the build backend and take longer than the rest. They skip, loudly,
when a wheel cannot be produced.

## A note on `OSS-n` in the tests

Several test modules and docstrings refer to `OSS-1` … `OSS-4`. Those name the
pre-release development stages this project went through before it was
published — separating code from vault, removing environment-specific
assumptions, proving portability, and packaging.

They are kept because they carry the *reason* a test exists. A line like "before
OSS-2, the content directories were a fixed tuple, so a remapped vault would
have indexed zero notes" explains what the test is defending against far better
than the assertion alone. You do not need to know the history to work on the
code — read them as "an earlier change", and the docstring will tell you which.

## Privacy expectations

This project came out of a private knowledge vault, and keeping the two apart
is a hard rule rather than a preference.

- **No private fixture data.** Test content must be obviously synthetic. There
  is a demo generator (`brain/demo.py`) whose fictional vault is a good source
  of examples; use it rather than inventing realistic-looking personal data.
- **No absolute home directories** anywhere in the package. A test asserts this
  over every module, and it will fail your PR.
- **No real paths, hostnames, account names, remotes or credentials**, in code,
  tests, fixtures, comments or commit messages.
- If you need to *name* a forbidden string — a leak scanner has to — put it in
  the one place that already owns that vocabulary rather than typing it again.

## Pull requests

- One change per PR, with a message explaining *why*, not only what.
- New behaviour comes with a test. Bug fixes come with a test that fails
  without the fix.
- Keep the full suite green, including on Python 3.11.
- Match the surrounding style: the code favours explicit, boring constructs and
  comments that explain reasoning rather than restating the line below.
- Do not add dependencies. One runtime dependency is a design constraint, not
  an accident. If something genuinely needs a second, argue for it in an issue
  first.
- Do not commit build output (`dist/`, `build/`, `*.egg-info/`).

## Breaking changes

Before 1.0 the CLI and configuration format may change. That is not a licence
to break things casually: a breaking change needs a reason, a changelog entry,
and — where it is cheap — a migration path or a clear error telling the user
what to do.

Note formats are held to a higher standard than the CLI. People's notes are the
point of the project; they should keep working.

## Licensing of contributions

By contributing you agree that your contribution is licensed under the
**GNU AGPL-3.0-or-later**, the same licence as the project. There is no
contributor licence agreement and no copyright assignment.

## Security

Do not report vulnerabilities through issues or pull requests. See
[SECURITY.md](SECURITY.md).
