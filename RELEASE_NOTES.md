Fix displaced reference wires and show Papan wires above the preview panels.

- Start wires at the visible output connectors in Nodes 2.0, instead of the hidden native slot positions.
- Keep IMAGE, VIDEO, and SECONDS wires aligned when selecting different references, changing boards, zooming, and reopening a workflow.
- Draw Papan wires above preview panels in both ComfyUI renderers, including dragging and H3's multiple-reference link format. Preserve native wire paths and reroutes.
- Let mouse clicks and scrolling pass through the wire layer. Clearing the workflow also clears the layer.

Download `comfyui-papan-v0.1.2.zip` from this release. Back up an existing ZIP installation, replace its extension folder under your active ComfyUI installation's `custom_nodes`, then refresh ComfyUI's window. Git installations can use `git pull --ff-only` and refresh. This update changes frontend files only; requirements are unchanged.

Verified with real wheel events, native resize handles, IMAGE/VIDEO/FLOAT dragging, reference and board switching, drawn endpoint measurements, wire pixels above previews, H3's saved multiple-reference link format, zoom/pan, and saved workflows in both renderers on ComfyUI Desktop's ComfyUI 0.39.0 / frontend 1.53.10, using an isolated CPU server and headless Chromium. Format and endpoint checks also pass. Windows and macOS have not been directly tested.
