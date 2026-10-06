First public release of Papan for ComfyUI.

- Open a `.papan` or portable `.papan.zip` file in one Papan Board node.
- Browse every image and video preview, with a password popup for protected files.
- Select references to add title-only IMAGE and VIDEO connectors below the gallery.
- Play selected video previews automatically; select again to deselect and disconnect.
- Connect a video's SECONDS output to a FLOAT duration input, including MiniMax H3 Easy's Seconds input.
- Keep reference connections when saving and reopening workflows.

Download `comfyui-papan-v0.1.0.zip`, extract it into the active ComfyUI installation's `custom_nodes` folder, install `requirements.txt` with that installation's Python, and restart ComfyUI. The SHA-256 file checks the ZIP download. Git installation instructions and a portable demo board are included in the README.

Verified with ComfyUI 0.37.0 / frontend 1.53.6 and ComfyUI Desktop's ComfyUI 0.39.0 / frontend 1.53.10 on Linux. The existing integration checks cover both canvas and Nodes 2.0 renderers, IMAGE/VIDEO/FLOAT connections, exact video duration, protected files, and workflow reloads. Windows and macOS installation instructions are provided; those platforms have not been directly tested for this release.
