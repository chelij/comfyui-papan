Papan for ComfyUI v0.1.4 connects directly to selected collections in Papan v0.3.2 or newer.

- **Live collections.** Copy a connection from Papan's **Connect apps** window, paste it into **Papan Board → Connect Papan…**, and choose a collection. Browse all shared previews and stream video through the ComfyUI backend.
- **Automatic updates.** Refresh after edits, downloads, pose extraction, locking, or access changes. Reconnect after service interruptions and replace keys without changing the workflow's reference identities.
- **Protected access.** Unlock live collections in Papan. Every workflow execution checks current access, including cached results; locking or revoking access stops further reading and playback. Connection keys stay in ComfyUI's server settings, outside workflows and media URLs.
- **Pose and video references.** Select extracted poses beside their original videos, preview clip durations, and keep VIDEO/SECONDS outputs and wires stable while resizing, switching boards, and reopening workflows. Saved-file workflows remain available.

Download `comfyui-papan-v0.1.4.zip`. Back up an existing ZIP installation and replace its folder under the active ComfyUI installation's `custom_nodes`. Git installations can use `git pull --ff-only`. Restart ComfyUI and refresh its window; this update changes both backend and frontend files. Requirements are unchanged.

Papan and the ComfyUI server must run on the same computer. The connection uses loopback and explicitly granted collections; saved files still work independently. Previously imported input files and downstream outputs remain client-owned copies and cannot be recalled by locking or revocation.

The release is checked with format/endpoint tests and isolated classic/Nodes 2.0 workflows covering connection setup, streaming, pose execution, locking, revocation, reconnects, and saved references. Exact versions, evidence, and remaining limits are recorded in the [ecosystem verification](https://github.com/chelij/papan/blob/main/docs/verification.md). Windows/macOS extension behavior and H3 generation quality remain unverified.
