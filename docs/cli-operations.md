# CLI operations and troubleshooting

## Known installation layout

The September 4, 2026 read-only discovery found all three coding hosts using `~/.local/bin/{codex,claude,grok}` and `~/.opencode/bin/opencode`. One host's resolved targets confirmed native standalone installations for Codex, Claude, and the official xAI Grok CLI. Git is `/usr/bin/git`.

Earlier sessions reported missing tools because noninteractive SSH did not inherit the same PATH as an interactive session. The command now checks known paths first and builds a complete PATH for subprocesses. It also searches configured user bin directories and installed NVM Node bin directories. It never guesses a replacement package from a tool's short name.

[Bash's startup-file documentation](https://www.gnu.org/software/bash/manual/html_node/Bash-Startup-Files.html) explains why shell startup behavior varies. The command avoids depending on it.

## Routine updates

1. Run `./fleet check --group coding` and inspect missing/broken tools.
2. Preview with `./fleet update --group coding --dry-run`.
3. Run `./fleet update --group coding` (or choose one host/tool).
4. Review the dated log. Active agent processes may continue using their already-loaded versions; restart those sessions when appropriate.

The inventory encodes the fleet's existing native self-updaters: `codex update`, `claude update`, `grok update`, and `opencode upgrade`. These commands were used in prior successful fleet sessions. `codex update --help` was additionally verified on a coding host on September 4, 2026; it explicitly describes updating to the latest version. The [general official Codex CLI page](https://learn.chatgpt.com/docs/codex/cli) did not establish that native updater command during this review, so installed help is the command-specific evidence. See the [Claude CLI reference](https://code.claude.com/docs/en/cli-usage) and [OpenCode upgrade reference](https://opencode.ai/docs/cli/#upgrade) for those commands.

This is an update workflow, not an automatic install or package-manager migration. A changed installation should be reflected in inventory before using the updater. OS packages such as Git are handled separately.

## T3 Code background-service updates

T3 Code is managed separately from the coding-agent CLIs above. `./fleet update --group coding` does not update the T3 Code application or its `t3code.service` user service. `./fleet services --group coding` does check them; each coding host runs one.

Finish active work and wait for any remote update already in progress before updating, because the operation restarts the server. Run every command as the normal T3 Code user, never as root.

### Register with T3 Connect

Register each coding host interactively. T3 Connect authorization is tied to the user running T3 Code, so connect over SSH as the same normal user that owns `t3code.service`. Do not run these commands with `sudo` or as root.

```sh
ssh <host>
t3 connect
```

The command prints a browser link and a short code. Open the link on any device, sign in to T3 Connect, confirm that the displayed code matches, and approve the host. The CLI notices the approval without an SSH port forward. If setup offers to install the background service, accept it only when `t3code.service` is not already installed; T3 Connect registration and service installation are separate.

`t3 connect login` only authenticates the CLI. It does not expose or register the environment, so use the full `t3 connect` setup above for a new host. Restart an existing service after setup so it loads the saved link:

```sh
t3 service restart
```

Repeat this process for every coding host. On the phone, web app, or desktop app, sign in to the same T3 Connect account and choose the newly registered environment.

Check the saved authorization and link configuration on the host, then check that the service is running:

```sh
t3 connect status
t3 service status
```

`t3 connect status` reports saved configuration, not live reachability. If the environment appears offline in a client, inspect `t3 service status` and its displayed log. For `auth_invalid` or `invalid_bearer`, refresh the login and restart the service:

```sh
t3 connect login
t3 service restart
```

If T3 Connect revoked the credentials, run `t3 connect logout`, repeat the full `t3 connect` setup, then restart the service.

To stop exposing the host while keeping its T3 Connect login, run `t3 connect unlink`. To remove the link and saved login, run `t3 connect logout`. Deregister an unused environment from the account's T3 Connect page to revoke its cloud access and free its host slot. Removing the background service is a separate operation.

Treat browser links and authorization codes as passwords. Never put them in `inventory.json`, screenshots, runbooks, or logs. The fleet command does not automate this interactive registration. See the [upstream remote-access guide](https://github.com/pingdotgg/t3code/blob/main/docs/user/remote-access.md) for client setup, revocation, and current troubleshooting steps.

### The update command

Updating the services is a fleet command, driven from `inventory.json`:

```sh
./fleet service-update --group coding --dry-run   # print the commands, run nothing
./fleet service-update --group coding             # update and verify
./fleet service-update --host workstation02                 # one host
```

It reports a before and after version per service, refuses to run from inside the unit's own cgroup, and treats a service that is not healthy afterwards as a failed update rather than a success. Results land in the dated logs like any other run. `./fleet update` covers the coding-agent CLIs only and never touches these services.

The method lives in the inventory rather than in this document. `service_updaters` declares how an updater is found and invoked, and each service names the one it uses:

```json
"service_updaters": {
  "t3": {"paths": ["~/.local/bin/t3"], "version_args": ["--version"],
         "update_args": ["update", "--channel", "nightly", "--yes"]}
},
"services": [
  {"name": "t3-workstation02", "host": "workstation02", "kind": "systemd_user",
   "unit": "t3code.service", "updater": "t3"}
]
```

To pin a release instead of following the channel, change `update_args` to `["update", "<exact-version>", "--yes"]` and rerun. `--yes` is required: without it the CLI waits for a prompt that a non-interactive run will never answer. Moving to an older release needs `--allow-downgrade`; treat a downgrade as a separate, deliberate maintenance operation.

Run by hand on a host, the same command is:

```sh
t3 update --channel nightly --yes
```

Since September 14, 2026 all three hosts carry the self-contained `t3` launcher at `~/.local/bin/t3`, symlinked into the version it runs out of `~/.t3/runtime/versions/`. It needs no Node, npm, or npx. `~/.local/bin` is on the login-shell PATH on all three and is already in the inventory's `path_dirs`, but a non-login `ssh host 'command'` does not necessarily get it, so use `ssh host 'bash -lc "t3 update ..."'` or the absolute path. `t3 service restart` picks up a version that `t3 update` installed but did not restart into.

`npx t3@<version> service update` still works but is deprecated upstream in favour of `t3 update`; it prints a deprecation notice and repairs the service. Prefer the launcher.

### Installing or repairing the launcher

The [upstream installer](https://t3.codes/install.sh) is plain `sh`, needs only tar and curl or wget, verifies a SHA256 from the release's `SHA256SUMS`, and unpacks into `$T3CODE_HOME/runtime/versions/<version>` — the same layout `t3 service install` uses, so it reuses a download the service already has instead of refetching it. It then symlinks `~/.local/bin/t3`. Review the script before running it; the fleet does not run installers unattended.

```sh
curl -fsSL https://t3.codes/install.sh -o t3-install.sh
less t3-install.sh
T3CODE_VERSION=<exact-version> sh t3-install.sh
```

`T3CODE_CHANNEL` (stable, nightly, preview) selects a train when `T3CODE_VERSION` is unset; `T3CODE_INSTALL_BIN_DIR` moves the symlink. The script refuses a preview build that was not explicitly requested.

If the target version directory already holds a matching `.install-complete`, the script skips the download and only rewrites the symlink. If it does not match, the script does `rm -rf` on that directory — so when a service is running out of it, confirm the marker first:

```sh
cat ~/.t3/runtime/versions/<version>/.install-complete
```

### Never update from inside a T3 Code session

`t3code.service` uses `KillMode=mixed`, so stopping the unit SIGKILLs every process in its cgroup. A shell running under a T3 Code session lives in that cgroup, so an update or a `systemctl --user restart t3code.service` issued from one kills its own `systemctl` mid-operation. `Restart=always` does not cover an explicit stop, so the service stays down, leaving a stale `~/.t3/runtime/.service-stopping` marker and an abandoned staging directory under `~/.t3/runtime/versions/`. This is what stopped a coding host on September 14, 2026.

Run the update from a login shell, a terminal outside the service, or an SSH session. Confirm before starting:

```sh
cat /proc/self/cgroup   # must not contain t3code.service
```

Recovery is `systemctl --user start t3code.service`; the launcher clears the stale marker itself on startup.

### Packaging changed at 0.0.41-nightly.20260914.1700

From that build, `t3` on npm is a thin launcher whose platform package (`@t3code/t3-linux-x64`) carries a self-contained executable and ships its native modules prebuilt in a bundled `node_modules` — `@ff-labs/fff-node`, `node-pty`, `msgpackr-extract`, and the rest. Nothing is compiled at install time, so the update no longer needs npm install scripts at all.

Comparing dependency lists across versions is therefore not a health signal. A build from this packaging legitimately reports no `dependencies`; that is the new layout, not a broken publish.

A partially extracted npx tree does look like a broken package. During the September 14 session an interrupted `npx` install left `node_modules/@ff-labs` empty, and the executable failed at runtime:

```
Error: Cannot find module '@ff-labs/fff-node'
```

The platform package is roughly 64 MB compressed and 205 MB unpacked, so an interrupted install is plausible and the resulting tree is silently incomplete. Confirm the bundled modules rather than blaming the release, and clear the cache entry to repair it:

```sh
ls ~/.t3/runtime/versions/<version>/node_modules/@ff-labs   # expect fff-node + fff-bin-*
rm -rf ~/.npm/_npx/<hash>                                   # then reinstall
```

Using the launcher instead of `npx` avoids this cache entirely.

### Historical: `~/.npmrc` allow-scripts and npm 12

Before the self-contained packaging, the updater ran `npm install --prefix <staging>` and needed build scripts for `node-pty` and `msgpackr-extract`. npm 12 rejects `--allow-scripts` and the `npm_config_allow_scripts` environment variable in project-scoped installs, and npm exports its own file config to child processes — so an `allow-scripts=` line in `~/.npmrc` reached that nested install as the forbidden variable and failed it:

```
npm error code EALLOWSCRIPTS
npm error --allow-scripts is not allowed in project-scoped installs.
```

Removing the line was not a fix at the time either: npm 12 blocks install scripts by default, so the staged runtime then installed with no `pty.node` and was unusable. The workaround was `env -u npm_config_allow_scripts` on the update command.

This no longer applies. The affected host's `~/.npmrc` was removed on September 14, 2026 once the prebuilt packaging made it unnecessary, so no coding host now carries one. Keep it that way: reintroducing `allow-scripts` there would break any project-scoped npm install run under a wrapping npm process.

### Verify after updating

```sh
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
systemctl --user show t3code.service \
  --property=LoadState,ActiveState,SubState,UnitFileState
loginctl show-user "$(id -un)" --property=Linger
```

Expected values are `LoadState=loaded`, `ActiveState=active`, `SubState=running`, `UnitFileState=enabled`, and `Linger=yes`. Confirm the committed version in `~/.t3/runtime/service-state.json`, check `NRestarts=0`, and check that no `.service-stopping` marker or `.staging-*` directory was left behind. `./fleet services --group coding` covers the unit and linger checks for all three hosts at once. See the [upstream background-service guide](https://github.com/pingdotgg/t3code/blob/main/docs/user/background-service.md) for install, update, uninstall, and recovery details.

## Failure handling

| Result | Meaning | Next action |
| --- | --- | --- |
| `host_key_untrusted` | SSH rejected a missing, changed, or unsupported host key | Compare the fingerprint with a trusted host console, then maintain known_hosts using normal SSH procedures |
| `authentication_failed` | Key-based login failed | Check SSH user, key selection, and authorized_keys |
| `unreachable` | SSH transport failed | Check DNS, management network, host availability, and SSH configuration |
| `remote_error` | Worker could not run or return a valid result | Verify `python3` is available to noninteractive SSH |
| `missing` | No configured executable or PATH match is usable | Check symlinks and recorded install paths; repair the intended installation |
| `check_failed` | Executable ran unsuccessfully, timed out, or produced no recognizable version | Run its `--version` directly and inspect the installation |
| `update_failed` | Updater returned nonzero, could not execute, or timed out | Inspect JSON exit code and final version, then diagnose the reported command |
| `verification_failed` | Updater exited successfully but the final executable/version check failed | Inspect launcher/target and rerun a read-only check before attempting repair |
| `busy` | Another fleet update holds the per-host user lock | Wait for that run, then check state |
| `transport_timeout` | Overall SSH time budget elapsed | Recheck remote state before retrying; the bounded remote operation may still finish |

For discovery diagnosis, compare the JSON `path` and `resolved_path`. Add a host-specific `tool_overrides` entry when a host intentionally differs. Do not treat stale or broken launchers as an instruction to install an unrelated tool with the same name.

Historical installer references: [OpenCode](https://opencode.ai/install) and [official xAI Grok](https://x.ai/cli/install.sh). Review installer contents and the current installation before a manual repair; fleet does not download or run installers. Preserve OpenCode's existing global provider/model configuration and user permissions on any host that has been tuned by hand.
