# Skills and instruction management

`fleet skills` and `fleet instructions` use the same host inventory, key-based SSH access, host filtering, concurrency, and dated logs as CLI maintenance. The workspace is the source of truth for content you explicitly assign.

The shared collection starts empty at your request. Nothing from the coding hosts or their existing skills managers has been imported, adopted, or redistributed. Existing skills remain visible in audit reports.

## Audit and preview

```sh
./fleet skills check
./fleet instructions check
./fleet skills sync --dry-run
./fleet instructions sync --dry-run
./fleet skills check --host workstation02 --agent codex
./fleet instructions check --host workstation02 --agent claude
```

`--host` and `--agent` are repeatable. Both commands default to the `coding` group and all four agents. Inventory selections must assign the selected agents to every selected host. Use `--group`, `--jobs`, `--timeout`, and `--log-dir` as needed. CLI update commands continue to work independently.

`check` audits known user directories and compares assigned content with the workspace. It reports missing files, changed content, conflicts, identical external copies, and previously managed content retained after its assignment was removed. A check against an empty catalogue is an audit only; an empty sync makes no remote content or ownership-state changes.

Each run writes `logs/content-<timestamp>-<id>.json` and `.md`. JSON contains whole-skill hashes, file locations, symlink flags, assigned content status, and backup paths, without copying instruction/skill bodies into the report. Markdown summarizes each host and agent. These reports stay separate from the CLI version reports.

## Add and assign a skill

Import a local skill directory, including its references, scripts, and assets:

```sh
./fleet skills import --source /path/to/my-skill
```

This copies the skill into `agent-content/skills/my-skill/` and registers it in [the manifest](../agent-content/manifest.json). It does not assign or deploy it. The source folder must contain `SKILL.md` with a matching `name` and a nonempty `description`. Names use lowercase letters/digits with single hyphen separators, up to 64 characters. The importer checks required scalar or block frontmatter fields, not every agent's full YAML schema.

Edit the manifest's existing agent entries to assign the skill. For example, set `agents.codex.skills` and `agents.claude.skills` to `["my-skill"]` to synchronize those two destinations. Set the corresponding arrays for Grok and OpenCode if they should receive native copies too. Existing directory/audit settings in each entry stay in place.

Then run:

```sh
./fleet skills validate
./fleet skills sync --dry-run
./fleet skills sync
./fleet skills check
```

Edit the workspace skill files to publish a subsequent revision through the same preview/sync workflow. This is a snapshot distribution mechanism; it does not pull upstream changes or install plugins. Review dependencies, tool availability, and frontmatter before assigning a skill to another agent. Agent-specific invocation flags are preserved and produce a portability notice when recognized; they are not translated into equivalent policies.

The complete skill tree is hashed, including executable bits. Relative references and script permissions are preserved. An explicitly imported root symlink is dereferenced into a copy; nested symlinks and special files are rejected. A skill can contain at most 5,000 files and 16 MiB; a selected bundle is also limited to 16 MiB. Authentication directories, agent settings, plugin caches, sessions, and shell environment files are not part of the sync protocol. Only the explicitly imported skill directory is copied, so review its contents before import.

## Add shared or agent-specific instructions

Write a Markdown file under `agent-content/instructions/`, or import an existing local file:

```sh
./fleet instructions import --source /path/to/coding-conventions.md --name common
```

To assign that file to every selected agent, add its catalogue-relative path to the manifest's `instructions.common` list:

```json
"instructions": {
  "common": ["instructions/common.md"]
}
```

For additional Claude-specific instructions, for example, create `agent-content/instructions/claude.md` and add `"instructions/claude.md"` to `agents.claude.instructions`. Common files are combined in list order, followed by the agent-specific files. No instruction text is supplied by default.

```sh
./fleet instructions validate
./fleet instructions sync --dry-run
./fleet instructions sync
./fleet instructions check
```

The resulting text is inserted into a clearly marked block in the agent's global instruction file. Existing text outside the block is preserved, including later host-specific edits. Sync updates only the managed block. A locally edited or removed managed block causes a conflict; it is never silently overwritten. Do not place fleet's reserved begin/end marker comments in source files.

This manages global user instructions. Repository instructions, managed enterprise policy, learned memory, hooks, permissions, and agent configuration remain outside its scope. File sync confirms content, not the instruction precedence or behavior of a running model.

## Agent locations and compatibility

| Agent | Managed skill directory | Managed global instruction file |
| --- | --- | --- |
| Codex | `~/.codex/skills/<name>/` | `~/.codex/AGENTS.md` |
| Claude | `~/.claude/skills/<name>/` | `~/.claude/CLAUDE.md` |
| Grok | `~/.grok/skills/<name>/` | `~/.grok/AGENTS.md` |
| OpenCode | `~/.config/opencode/skills/<name>/` | `~/.config/opencode/AGENTS.md` |

These are explicit adapter settings in the manifest, relative to the SSH user's home. This fleet uses Codex's existing `~/.codex/skills` layout, observed in the installed local skill catalogue. Current [official Codex skill documentation](https://learn.chatgpt.com/docs/build-skills) lists `~/.agents/skills` as the user location; both directories are audited. Change the Codex destination only when deliberately migrating that layout, and inspect duplicates before doing so. Codex's [global instruction rules](https://learn.chatgpt.com/docs/agent-configuration/agents-md) give a nonempty `AGENTS.override.md` precedence; the instruction sync reports that as a conflict.

Claude's native user skill directory and instruction file are documented in [skills](https://code.claude.com/docs/en/skills) and [memory](https://code.claude.com/docs/en/memory). Grok documents native and compatibility skill roots in [skills/plugins](https://docs.x.ai/build/features/skills-plugins-marketplaces), and global instructions in [project rules](https://docs.x.ai/build/features/project-rules). OpenCode documents its native and compatibility paths in [skills](https://opencode.ai/docs/skills/) and [rules](https://opencode.ai/docs/rules/).

Grok and OpenCode can discover Claude-compatible and `.agents` skills. Consequently, per-agent assignment controls where fleet writes, not which agents are allowed to see a skill. Identical content can appear in multiple audited roots; observation counts are directory entries, not unique or enabled skills. Global Claude/Grok Markdown rule directories are also audited. Project/plugin skills and arbitrary custom discovery paths are not exhaustively enumerated.

The audit used the hosts' default user layouts. If you use `CODEX_HOME`, `CLAUDE_CONFIG_DIR`, XDG overrides, custom skill paths, or disabled skills, review the manifest paths and actual agent configuration before deployment. The tool intentionally does not source interactive shell profiles or rewrite agent settings to force discovery. Native `/skills` or agent inspection commands and a fresh session provide a final application-level check after a real deployment.

## Ownership, conflicts, and backups

| Result | Meaning |
| --- | --- |
| `in_sync` | Managed content matches the workspace |
| `external_identical` | Matching unmanaged content exists and remains externally owned |
| `missing` | Assigned destination is absent |
| `outdated` | Managed destination matches the last deployed version; workspace has changed |
| `planned` | Preview would create, update, adopt, or remove the item |
| `synced` | Content was written and verified |
| `conflict` | Unmanaged content differs, local managed edits exist, or instruction precedence would shadow the managed text |
| `removed` | Explicit pruning backed up and removed an unchanged managed item |

By default, sync does not replace a differing unmanaged skill. To deliberately move selected existing skill destinations under fleet ownership, inspect the catalogue and preview:

```sh
./fleet skills sync --host workstation01 --agent codex --adopt --dry-run
./fleet skills sync --host workstation01 --agent codex --adopt
```

`--adopt` covers assigned skills for the selected agents/hosts. It backs up the destination and installs a managed copy, including when an external copy was identical. For a symlink, the link itself is backed up; its external target remains untouched. It does not override local edits to an already managed regular skill. Avoid simultaneous edits by another skills manager after adopting the same destination.

Before writing, sync checks all selected destinations on a host for conflicts. Preflight conflicts skip that host's content writes. Other hosts proceed independently. Each file/tree is verified after writing; completed items are recorded individually in ownership state. This is not an all-host transaction: an I/O failure or dropped connection can leave part of a run completed. Recheck before retrying.

Each host records ownership at `~/.local/state/fleet-management/content/state.json`. A per-user lock serializes content syncs. Existing content that will change is preserved under `~/.local/state/fleet-management/content/backups/<run-id>/<destination>`. Report entries identify exact backups. Staged skill directories and atomic instruction-file replacement limit incomplete writes; destination symlink ancestors are rejected.

## Unassign and prune

Removing a manifest assignment does not silently delete files. Previously managed content is reported under `retained_managed` and the agent is marked as drifted until you decide what to do.

After reviewing the assignments, use:

```sh
./fleet skills sync --prune --dry-run
./fleet skills sync --prune
./fleet instructions sync --prune --dry-run
./fleet instructions sync --prune
```

Pruning only considers previously managed destinations belonging to the selected agent adapters. It backs up unchanged skill directories before removing them. For instructions, it removes only the managed block and preserves surrounding personal text. Locally edited managed content remains a conflict. Unmanaged skills are never pruned. If you change an adapter's destination directory, its former paths are outside the new adapter's prune scope; handle that as a deliberate migration.

## Recovery

For an ordinary content rollback, restore the previous source revision in the workspace and sync again; this keeps ownership state consistent and retains another backup. This directory is not currently a Git repository, so retain source revisions yourself if you need that workflow.

For a manual restore from a host backup, pause concurrent syncs, preserve the current destination separately, restore the exact reported backup to its destination, and review that destination's ownership entry in `state.json`. Remove its ownership entry if restoring an externally managed directory or symlink; never delete unrelated entries. The next check may correctly report a conflict until the workspace source matches the restored content. There is no automated rollback command.

## Validation performed

Automated tests cover empty collections, dry-run immutability, idempotent sync, script permissions/references, local drift, symlink preservation/adoption, preflight conflicts, instruction merging, backups, pruning, checksums, and transport serialization. Live checks audit all four agents on the coding hosts. Deployment tests use temporary directories and sample content, keeping the real shared collection empty.
