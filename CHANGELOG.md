# Changelog

## 0.1.5 - 2026-10-17

Changes since [v0.1.4](https://github.com/chelij/comfyui-papan/releases/tag/v0.1.4): [full comparison](https://github.com/chelij/comfyui-papan/compare/v0.1.4...v0.1.5) · [download the diff](https://github.com/chelij/comfyui-papan/releases/download/v0.1.5/comfyui-papan-v0.1.4-to-v0.1.5.diff).

### Fixed

- Reject non-string identifiers consistently across saved boards, encrypted media, live connections and workflow references.
- Fall back to a readable video preview when the original file contains only audio.
- Return a structured HTTP 400 for malformed or non-object JSON in board-open, unlock and connection requests, preserving existing connection state.
- Refresh saved-copy cache fingerprints when imported files change or disappear and ComfyUI supplies an empty prompt. Live references still check current collection access on every execution.

### Verification

- Recover saved/live workflow checks when ComfyUI's startup Templates dialog appears after bootstrap. Forced late-dialog checks pass in classic and Nodes 2.0; this is a test-harness fix, with no frontend runtime change.
- The backend suite now has 25 format/endpoint/node tests. Main checks use the immutable desktop 0.3.4 candidate; tag checks use published desktop v0.3.4, on Python 3.11 and 3.13.
- Linux saved/live workflows retain stable IMAGE/VIDEO/SECONDS identities, full 51-frame pose execution, protected cached-prompt denial, restarts and revocation. v1 file/library contracts and requirements remain compatible. Live connections still require desktop 0.3.2+; saved files work independently.
- Windows/macOS extension behavior and H3 generation quality remain unverified. [Ecosystem evidence](https://github.com/chelij/papan/blob/main/docs/verification.md#reliability-releases-17-october-2026), [component #9](https://github.com/chelij/comfyui-papan/issues/9), [coordinator #10](https://github.com/chelij/papan/issues/10).

## 0.1.4 - 2026-10-14

Introduced the live desktop-library adapter, automatic refresh and protected execution checks, retaining saved-file workflows and stable pose/video references. [Original release notes](https://github.com/chelij/comfyui-papan/releases/tag/v0.1.4).
