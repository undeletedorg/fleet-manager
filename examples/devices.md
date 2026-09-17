# Devices

Generated from [inventory.json](inventory.json). Edit the inventory, then run `./fleet render`.

| Name | SSH destination | Groups | Expected tools | Services |
| --- | --- | --- | --- | --- |
| workstation01 | youruser@workstation01.mgmt.example.internal | coding | codex, claude, grok, opencode, git | — |
| workstation02 | youruser@workstation02.mgmt.example.internal | coding | codex, claude, grok, opencode, git | [t3-workstation02](docs/services/t3-connect.md) |
| compose01 | root@compose01.mgmt.example.internal | docker |  | [example-app](docs/services/example-app.md) |

## Host notes

- **workstation01:** Example coding host. User-level tools; no sudo needed for CLI updates.
- **workstation02:** Example coding host running the background service.

See [CLI operations and observations](coding-agents-cli.md) and the [operations guide](README.md).
