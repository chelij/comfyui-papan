Papan for ComfyUI v0.1.5 improves saved-reference recovery and input validation.

Changes since **v0.1.4**:

- Reject non-string identifiers consistently in saved boards, encrypted media, live connections and workflows.
- Use a readable video preview when the saved original contains only audio.
- Return a structured HTTP 400 for malformed or non-object JSON without replacing existing connection state.
- Refresh saved-copy cache fingerprints when imported files change or disappear, including ComfyUI's empty-prompt checks. Live references continue checking access on every execution.
- Make the saved/live test harnesses recover from a late startup Templates dialog. Forced-dialog checks pass in classic and Nodes 2.0; production frontend code is unchanged.

[Changelog](https://github.com/chelij/comfyui-papan/blob/v0.1.5/CHANGELOG.md) · [Full comparison](https://github.com/chelij/comfyui-papan/compare/v0.1.4...v0.1.5) · [Download the diff](https://github.com/chelij/comfyui-papan/releases/download/v0.1.5/comfyui-papan-v0.1.4-to-v0.1.5.diff)

Download `comfyui-papan-v0.1.5.zip` and check its `.sha256` file. Back up an existing ZIP installation and replace its folder under the active ComfyUI installation's `custom_nodes`. Preserve any personal extension customizations. Git installations can use `git pull --ff-only`. Restart ComfyUI and refresh its window. Requirements are unchanged.

Publication requires all 25 format/endpoint/node tests on Python 3.11 and 3.13. Main checks use the immutable desktop 0.3.4 candidate; tag checks use [published desktop v0.3.4](https://github.com/chelij/papan/releases/tag/v0.3.4). Linux saved/live evidence covers classic and Nodes 2.0, full 51-frame pose execution, protected cached-prompt denial, automatic updates, restarts, revocation and stable workflow wires. Public ZIP/checksum and all payload files are verified. [Exact checks and limits](https://github.com/chelij/papan/blob/main/docs/verification.md#reliability-releases-17-october-2026).

Saved file formats and library protocol remain v1. Live connections still require Papan 0.3.2+ on the same computer; saved files work without a running desktop. Unlock protected live collections in Papan. Connection keys stay in ComfyUI server settings, outside workflows; earlier imported files and outputs remain client-owned copies. Windows/macOS extension behavior and H3 generation quality retain their existing verification limits.

Licensed GPL-3.0-or-later. Source and license accompany the extension ZIP.
