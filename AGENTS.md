# AGENTS.md

Fleet management for a small lab fleet: check and update the coding-agent CLIs
and background services on remote hosts over SSH, from one inventory file.

## Purpose

`inventory.json` is the source of truth — hosts, groups, tools, updater
arguments, services. Everything else is derived from it: `devices.md` and
`coding-agents-cli.md` are generated, and every run writes a dated log into
`logs/`.

Nothing is installed on the targets. `./fleet` opens SSH, sends a self-contained
Python worker on stdin, and reads one JSON result back. It runs as the inventory
SSH user without sudo, and it never downloads or runs installers.

## Using it

```sh
./fleet --help      # authoritative command list
./fleet validate    # after any inventory.json edit
```

`check` and `update` act on CLI tools. `services` and `service-update` act on
registered services. `render` regenerates the docs without contacting hosts.
`--dry-run` prints the exact commands without running them; reach for it first.

`README.md` has the full workflow. `docs/cli-operations.md` has the failure
statuses and repair steps, and is where an unexpected result gets diagnosed.

## Local data versus tracked code

`.env` and `inventory.json` are local and git-ignored, along with `devices.md`,
`coding-agents-cli.md`, `logs/`, and `docs/services/` — they name real hosts,
users, and addresses. Sanitized equivalents are tracked under `examples/`, and
the test suite loads `examples/inventory.json` so it passes on a fresh clone.

Identifying values live in `.env`; the inventory refers to them as
`${FLEET_DOMAIN}` and friends. Put a new one in `.env` and `.env.example`
together, never a literal hostname in the inventory.

## Conventions

- **Edit `inventory.json`, not the generated docs.** `devices.md` and
  `coding-agents-cli.md` are overwritten on the next run. Pinning a version,
  changing a release channel, or adding a host is a config edit, not a code
  edit — that is the point of the design.
- **Run `./fleet validate` after editing it,** then `./fleet render`.
- **Keep credentials out** of `inventory.json`, `logs/`, and the runbooks.
  Updater output is deliberately never persisted, because it can carry account
  and relay details.
- **Treat `logs/` as append-only.** They are dated run observations, and the
  generated docs read history back out of them.
- **Tests:** `python3 -m unittest discover -s tests` (pytest is not installed).

## One hazard worth knowing

`t3code.service` on the coding hosts uses `KillMode=mixed`: stopping the
unit SIGKILLs every process in its cgroup. An agent session running *inside* T3
Code on one of those hosts that restarts or updates that service kills itself
partway through and leaves the service down. This has happened.

Run it from an SSH session or another terminal, and confirm first:

```sh
cat /proc/self/cgroup   # must not contain t3code.service
```

`./fleet service-update` refuses this case on its own. A hand-typed
`systemctl --user restart` does not.

## A separate thing with a similar name

`agent-content/` distributes skills and instruction files *to* agents across the
fleet (`./fleet skills`, `./fleet instructions`). It does not configure this
repository. See `docs/agent-content.md`.
