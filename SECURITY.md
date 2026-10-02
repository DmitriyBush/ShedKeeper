# Security policy

ShedKeeper guards a person's files and databases against the mistakes of an AI coding agent. A flaw in it can
mean lost data, so reports are welcome and are taken seriously.

## How to report a vulnerability

**Please do not open a public issue for a vulnerability.** Report it privately:

1. Open the **Security** tab of this repository on GitHub.
2. Choose **Report a vulnerability** (GitHub's private vulnerability reporting; it creates a private security
   advisory that only you and the maintainers can see).

If the tab offers no **Report a vulnerability** (private reporting is a setting of the repository and may be off), open an ordinary issue titled
**security contact** and put NO details of the flaw into it -- only that you have one to report. The maintainer will then arrange a private way to receive it.

Say what you did, what you expected, what happened, the version (`VERSION`), the Claude Code version, and
whether the plugin or `install.py` was the way in. A short command line that reproduces it is the best report.
**Do not put a real password, key or private data into a report** -- use a made-up database and made-up files.

## What counts as a vulnerability here

ShedKeeper is a helper for an agent that works in good faith and can make a mistake; it is not a lock against
an agent that was taught to do harm (see "Risks" in the [README](README.md)). Inside that promise, these are
vulnerabilities:

- **A bypass of the copy.** A change to an existing file, or an action that destroys data in a PostgreSQL or
  SQLite database, goes through with no verified copy, in a form the README says is protected.
- **A weakening of the protection by the agent alone.** A setting that lets something through, or the removal of
  the hooks, takes effect without the confirmation code the person types, by a route the README says is closed.
- **A leaked password or secret.** A database password printed, written into a record, a log, a copy list or a
  command line of a child process.
- **A false "protected".** ShedKeeper says a copy was taken, a restore was clean, an engine is proven or the
  protection is on, and it is not true.
- **A way for the agent to write into the store** (the copies, the trash, the journals) or to make the person's
  confirmation code appear without the person typing it.
- **A restore that overwrites** a live file or database it should have left alone.

Not vulnerabilities, because they are declared limits (README, "What ShedKeeper does NOT see" and "Risks"; and
ARCHITECTURE.md, section 8): a program that destroys data by itself (`python script.py`); a command made confusing
on purpose; a mod of Claude Code; an administrator of the machine; MySQL and other engines nobody has written an
adapter for. A report that shows one of these is still useful if the README does not name it -- then the
README is what is wrong.

## Supported versions

| Version | Supported |
|---|---|
| 0.3.x | yes -- fixes go here |
| 0.2.x, 0.1.0 | no -- update to 0.3.x (the README section "Updating from 0.2.0" says what changes) |

## After a report

When a fix is released, the release notes ([CHANGELOG.md](CHANGELOG.md)) say what was wrong, without the details
that would help to use it against people who have not updated yet.

No response time is promised: a report is read and answered when the maintainer can.
