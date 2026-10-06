# Papan ecosystem upkeep

- Before changing board formats, unlocking, saved workflows, reference outputs, compatibility, or releases, read the [Papan ecosystem overview](https://github.com/chelij/papan/blob/main/docs/ecosystem.md) and follow its tracking and upkeep rules. In sibling checkouts, use `../papan/docs/ecosystem.md`.
- This repository owns extension runtime, tests, and releases. Desktop owns the file formats; `papan/extensions/comfyui-papan` is a migration pointer. File extension tasks here and link cross-component work to one coordinating issue in `chelij/papan`.
- Work affecting another component is complete when the overview and coordinating issue record the contract/compatibility change, exact checked versions, evidence, and limits. Published releases also update the inventory.
- When updating native Papan test fixtures, preserve the source commit and scope in `tests/fixtures/README.md`. Use the README's affected checks; workflow persistence includes output identities and connections.
