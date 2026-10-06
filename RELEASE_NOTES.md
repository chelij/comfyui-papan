Fix resizing and mouse-wheel scrolling in the Papan Board node.

- Drag the node corner to enlarge or shrink the preview gallery in both classic ComfyUI and Nodes 2.0.
- Scroll the previews with the mouse wheel without zooming or panning the workflow, including at the gallery's edges.
- Keep the chosen node size when selecting references and reopening saved workflows. Grow only when selected output rows need more space.
- Keep title-only IMAGE, VIDEO, and SECONDS connectors below the gallery, with their connections preserved.

Download `comfyui-papan-v0.1.1.zip` from this release. Back up an existing ZIP installation, replace its extension folder under your active ComfyUI installation's `custom_nodes`, then refresh ComfyUI's window. Git installations can use `git pull --ff-only` and refresh. This update changes frontend files only; requirements are unchanged.

Verified with real wheel events, native resize handles, IMAGE/VIDEO/FLOAT dragging, gallery-edge scrolling, and saved workflow dimensions in both renderers on ComfyUI Desktop's ComfyUI 0.39.0 / frontend 1.53.10, using an isolated CPU server and headless Chromium. Format and endpoint tests also pass. Windows and macOS have not been directly tested.
