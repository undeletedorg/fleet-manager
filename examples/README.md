# Examples

Sanitized stand-ins for the files `.gitignore` keeps out of the repository,
because the real ones name actual hosts, users, and addresses.

| File | Stands in for |
| --- | --- |
| `inventory.json` | `inventory.json` |
| `devices.md` | `devices.md` |
| `coding-agents-cli.md` | `coding-agents-cli.md` |
| `logs/` | `logs/` |

To start a real fleet from here:

```sh
cp ../.env.example ../.env          # fill in your domain and SSH user
cp inventory.json ../inventory.json # then edit for your hosts
cd .. && ./fleet validate
```

`devices.md`, `coding-agents-cli.md`, and `logs/` are produced by the same code
that writes the real ones, so they show the true output shape. They are verbatim
copies of files that live in the repository root, so the relative links inside
them (`docs/…`, `inventory.json`) resolve from the root, not from this folder.

The runbook paths in `inventory.json` — `docs/services/example-app.md` and
`docs/services/t3-connect.md` — are illustrative. You write those runbooks
yourself; `docs/services/` is git-ignored for the same reason the inventory is.
