# Security policy

## Supported versions

Sako Brain is pre-1.0. Only the latest release receives fixes.

| Version | Supported |
|---|---|
| 0.1.x | Yes |
| Older | No releases exist |

## Reporting a vulnerability

**Please do not open a public issue, discussion or pull request for a security
problem.** That includes anything that would expose the reporter's own data.

Use **GitHub's private vulnerability reporting** on this repository: open the
repository's *Security* tab and choose *Report a vulnerability*. The report is
visible only to the maintainer until a fix is published.

This channel is enabled on the canonical GitHub repository at public launch. If
you are reading this from a mirror or a fork, report to the upstream project
rather than here.

## What to report

Beyond ordinary vulnerabilities, this project cares about two categories that
are easy to overlook:

- **Accidental exposure of private data** — if a release, artefact, generated
  file or documentation page contains a real path, hostname, account name or
  anything else that should not be public. The project has automated scanning
  for this, and a finding means the scanning failed.
- **Unsafe backup or destructive behaviour** — anything that could overwrite,
  delete or leak a user's vault, or write a credential to an unexpected place.
  `brain init` and the demo generator are held to "never destroy what they did
  not create"; a counterexample is a security bug.

## What to include

Whatever you have: the version (`brain --version`), your platform, what you
did, what happened, and what you expected. A minimal reproduction helps more
than anything else. Please do not include real personal data in the report.

## What to expect

This is a personal project with best-effort support and no service level. A
report will be read and acknowledged when the maintainer sees it. If a fix is
warranted, it lands in the next release and the advisory credits you unless you
ask otherwise.

## Scope

In scope: the `brain` package, its CLI, packaging and released artefacts.

Out of scope: vulnerabilities in Python itself, in PyYAML, or in the optional
external tools (`git`, `restic`, `rclone`, `systemd`) — report those upstream.
Also out of scope: a user configuring their own vault or backup insecurely,
unless the tool made that outcome likely or silent.
