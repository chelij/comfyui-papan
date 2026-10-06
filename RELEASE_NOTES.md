Fix wires starting in the middle of the output buttons.

- Anchor IMAGE, VIDEO, and SECONDS wires to the visible dots at the right of their buttons.
- Keep the dots aligned with wires when selecting references, switching boards, zooming, and reopening saved workflows.
- Retain the wire layer above previews and existing output mappings.

Download `comfyui-papan-v0.1.3.zip` from this release. Back up an existing ZIP installation, replace its extension folder under your active ComfyUI installation's `custom_nodes`, then refresh ComfyUI's window. Git installations can use `git pull --ff-only` and refresh. This update changes frontend files only; requirements are unchanged.

Verified with actual drawn wire endpoints, IMAGE/VIDEO/FLOAT dragging, reference and board switching, zoom/pan, saved workflows, and wires above previews in both ComfyUI renderers using an isolated CPU server and headless Chromium. Windows and macOS have not been directly tested.
