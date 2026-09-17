# Coding CLI operations

Generated from [inventory.json](inventory.json) and dated JSON run logs. Run `./fleet check` to refresh observations.

## Usage

```sh
./fleet check --group coding
./fleet update --group coding --dry-run
./fleet update --group coding
./fleet service-update --group coding
```

`check` reads installed versions; it does not query release servers or update tools.
`update` invokes the configured self-updaters and verifies the final versions. Git is check-only.

## Installation methods

| Tool | Installation | Update arguments | Preferred paths |
| --- | --- | --- | --- |
| codex | native standalone; built-in updater | codex update | ~/.local/bin/codex |
| claude | native installation; self-updater | claude update | ~/.local/bin/claude |
| grok | official vendor native CLI | grok update | ~/.local/bin/grok, ~/.grok/bin/grok |
| opencode | official curl installer; self-updater | opencode upgrade | ~/.opencode/bin/opencode, ~/.local/bin/opencode |
| git | OS package; checked here, updated through OS maintenance | OS maintenance only | /usr/bin/git |

## Background service updates

Run `./fleet service-update --group coding` (add `--dry-run` to print the commands first).
The updater is refused if the remote worker is inside the unit's own cgroup, and the
service must be healthy again afterwards or the update is reported as failed.

| Service | Host | Unit | Update command | Launcher |
| --- | --- | --- | --- | --- |
| t3-workstation02 | workstation02 | t3code.service | t3 update --channel nightly --yes | ~/.local/bin/t3 |

**t3:** self-contained launcher; needs no Node or npm. Use ["update", "<exact-version>", "--yes"] to pin a release.

## Latest observations

Each row retains its own observation time. A failed SSH attempt does not refresh old versions.

| Host | Tool | Observed at (UTC) | Status | Version | Executable |
| --- | --- | --- | --- | --- | --- |
| workstation01 | codex | 2026-01-01T09:00:00.000000+00:00 | unchanged | 0.154.0 | /home/youruser/.local/bin/codex |
| workstation01 | claude | 2026-01-01T09:00:00.000000+00:00 | updated | 2.1.270 | /home/youruser/.local/bin/claude |
| workstation01 | grok | never | not_checked | — | — |
| workstation01 | opencode | never | not_checked | — | — |
| workstation01 | git | 2026-01-01T09:00:00.000000+00:00 | check_only | 2.53.0 | /usr/bin/git |
| workstation02 | codex | 2026-01-01T09:00:00.000000+00:00 | unchanged | 0.154.0 | /home/youruser/.local/bin/codex |
| workstation02 | claude | 2026-01-01T09:00:00.000000+00:00 | updated | 2.1.270 | /home/youruser/.local/bin/claude |
| workstation02 | grok | never | not_checked | — | — |
| workstation02 | opencode | never | not_checked | — | — |
| workstation02 | git | 2026-01-01T09:00:00.000000+00:00 | check_only | 2.53.0 | /usr/bin/git |

## Latest host attempts

| Host | Attempted at (UTC) | Status |
| --- | --- | --- |
| workstation01 | 2026-01-01T09:00:00.000000+00:00 | ok |
| workstation02 | 2026-01-01T09:05:00.000000+00:00 | ok |
| compose01 | 2026-01-01T09:00:00.000000+00:00 | host_key_untrusted |

See [logs](logs/) for before/after versions, commands, and failures; see [CLI troubleshooting](docs/cli-operations.md) for repair guidance.
