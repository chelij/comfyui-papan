# Optional video helpers

These six nodes adapt native ComfyUI VIDEO references for frame-based motion workflows. **Papan Board does not need them.** Use ComfyUI's built-in **Get Video Components**, **Create Video**, **Trim Video**, **Remove Background**, **Image Composite Masked** and **Save Video** when those already do what you need. These helpers add a VIDEO adapter for VideoHelperSuite, LTX frame-count padding, bounded temporary batches, a simple subject-framing operation and an audio packet-copy saver.

All six appear under **Papan → Video helpers**. **Papan Board** stays directly under **Papan**. They accept ordinary ComfyUI media types; they do not require a live desktop connection and do not implement a motion model, pose detector or ControlNet.

## Install once, only if needed

The repository's `__init__.py` deliberately does **not** import these nodes. Installing or updating Papan Board adds no new registrations or requirements.

1. Use the `custom_nodes` directory belonging to the ComfyUI instance you actually run.
2. Copy [`optional_nodes/papan_motion_video.py`](../optional_nodes/papan_motion_video.py) to `custom_nodes/papan_motion_video.py`. Keep the filename exactly as shown. The copy inside the Papan repository is an inert source file.
3. If that standalone file already exists, back it up outside all active `custom_nodes` directories, then replace it. Do not install a second copy under another filename or import it from another extension. Duplicate node IDs make registration order decide which implementation runs.
4. When no generation is running, restart ComfyUI and refresh the browser. A browser refresh alone does not reload Python nodes.

Use the same replacement step for subsequent helper updates; updating the Papan repository alone does not refresh an already copied standalone file. To remove the helpers, remove only the standalone copy and restart when idle. Workflows containing their node IDs need the file installed to execute. Their IDs, input names, output order and menu category retain the earlier local helper contract.

This is an optional source addition, not a new released extension version. Existing release ZIPs do not contain it. The package builder includes the inert source and this guide for a future release, without enabling it.

## Dependencies and memory

| Dependency | Used by | Installation impact |
| --- | --- | --- |
| ComfyUI with native VIDEO, `VIDEO.save_to(..., codec, crf, preset)` and the V3 `ImageCompositeMasked.execute(...).result` API | Loader, compositor, saver | Use a recent ComfyUI; older API variants are not claimed. |
| PyTorch, SciPy and PyAV with H.264 encoding support | Frame operations, connected-component masks, saving | Already part of the checked ComfyUI requirements. This project adds no pip requirements. |
| [VideoHelperSuite](https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite) (`VHS_LoadVideoPath`) | **Only** the motion video loader | Install separately if you use that node. The other five helpers remain usable without it. Missing VHS gives an actionable execution error. |
| A compatible `BACKGROUND_REMOVAL` model from ComfyUI's **Load Background Removal Model** | Foreground masks and framing | Obtain a model separately and check its own license and hardware needs. These helpers download nothing and ship no models. |

Batching limits the frames passed to each mask/composite call. Complete input and output tensors still occupy memory; final masks/composites are collected on the CPU. A ten-minute video can exhaust host RAM even with `batch_size=1`. Model weights and other nodes have their own VRAM requirements. Loading or executing this file performs no network requests or package/model installation.

## Load motion driving video (Papan)

**Node ID:** `PapanMotionVideoLoader`

Converts a file-backed VIDEO into frame tensors through VideoHelperSuite's existing loader, with resampling and size controls. Use it when a motion node expects IMAGE frames and you need VHS's controls in the same graph. For a straightforward split at the video's existing rate, built-in **Get Video Components** is sufficient.

**Inputs:** `video` (VIDEO), VHS `force_rate`, `custom_width`, `custom_height`, `frame_load_cap`, `skip_first_frames`, `select_every_nth`, and optional VHS `format`. Hidden VHS inputs are retained. Defaults are 30 fps, width 640, no frame cap and format `None`; height is determined by VHS. `force_rate=0` follows VHS source-rate behavior. The adapter removes VHS's meta-batch and VAE controls because it returns IMAGE frames, not latent batches.

**Outputs, in order:** IMAGE frames, INT `frame_count`, AUDIO, and `VHS_VIDEOINFO` `video_info`.

**Example:** `Papan Board VIDEO → loader.video`; connect its IMAGE output to a frame-based motion/pose conditioning node. Set the downstream frame rate to the same rate used by the loader. Its AUDIO output is available to a native **Create Video** node.

**Limits:** requires VHS, a readable file-backed VIDEO and enough host memory for the returned frames. A virtual/trimmed VIDEO must first be saved as a new file; reading its underlying path would otherwise bypass its trim. Skipping frames, selecting every nth frame, a frame cap or a resampling rate changes the sequence. The helper does not infer the correct motion model settings.

## Full reference timing (LTX padding)

**Node ID:** `PapanMotionFrameTiming`

Repeats the final frame until there are enough frames for the reference duration and a count of `8n+1`. Use it for an LTX pipeline that requires that frame-count rule. It never drops supplied frames.

**Inputs:** `images` (IMAGE batch), `reference` (VIDEO) and `fps` (FLOAT, default 30). Set `fps` to the actual frame rate represented by `images`, not the source file's rate if you resampled it.

**Outputs:** IMAGE `padded_motion`, FLOAT `reference_duration` in seconds.

**Example:** `loader.IMAGE → timing.images`, `Papan Board VIDEO → timing.reference`, `timing.padded_motion → your LTX motion input`. At 24 fps, an 18-frame input for a 0.75-second reference becomes 25 frames. Set generation length from the padded frame count; use the original reference duration to trim the final output.

**Limits:** padding freezes the last frame; it does not interpolate motion, resample time, transfer poses or configure an LTX model. Other models may use a different frame-count rule. If the supplied sequence is already longer than the reference, it is retained and padded further as needed. Output length is intentionally longer than the final reference duration.

## Automatic video foreground masks in batches

**Node ID:** `PapanVideoForegroundMask`

Runs a background-removal model on every frame in small calls. Use it instead of passing a long video batch to **Remove Background** at once when temporary model inputs are too large.

**Inputs:** `bg_removal_model` (BACKGROUND_REMOVAL), `image` (IMAGE batch) and `batch_size` (INT, default 8).

**Output:** MASK batch with the same frame order, collected in CPU memory.

**Example:** `Load Background Removal Model → masks.bg_removal_model`, `loader.IMAGE → masks.image`, `masks.MASK → compositor.mask`.

**Limits:** this is independent per-frame segmentation. It adds no temporal smoothing or subject identity tracking; flickering or missed limbs depend on the selected model. Lower batch size reduces temporary work, not model weights or the total output size. It does not load a mask model itself.

## Match target framing to reference video

**Node ID:** `PapanMatchReferenceFraming`

Makes one reference-sized, white-background target image. It selects the largest connected foreground component in the first reference frame and first target image, scales the target subject to the reference subject's height, and aligns its top and horizontal center. Use it to prepare a cropped character image for a motion-conditioned generation.

**Inputs:** `target_image` (IMAGE), `reference_image` (IMAGE), `bg_removal_model` (BACKGROUND_REMOVAL), `mask_threshold` (FLOAT, default 0.5), `size_multiplier` (FLOAT, default 1.0), and optional `reference_mask` (MASK).

**Output:** one IMAGE, `aligned_target`, with the reference frame's dimensions. Crop the target to a single intended subject before connecting it.

**Example:** `Load Image → framing.target_image`, `loader.IMAGE → framing.reference_image`, and a loaded background-removal model → `framing.bg_removal_model`. Connect `aligned_target` to your generator's target/reference-image input. A hand-edited first-frame mask can select the reference subject through `reference_mask`.

**Limits:** uses only the first image/frame/mask. It matches a bounding box, not anatomy, camera perspective, pose or identity. Large disconnected body parts may not be included in the largest component. The subject is placed on white; existing background and alpha are not preserved. Bottom overflow is limited; horizontal overflow is cropped. Review the image before generation. An empty foreground produces an actionable error.

## Composite video frames in batches

**Node ID:** `PapanVideoCompositeMasked`

Calls ComfyUI's **Image Composite Masked** on smaller batches, preserving frame pairing. Use it for a long frame sequence when compositing the whole sequence in one core call creates too much temporary memory.

**Inputs:** `destination` (IMAGE), `source` (IMAGE), `x`/`y` placement (INT), `resize_source` (BOOLEAN), `batch_size` (INT, default 8), and optional `mask` (MASK).

**Output:** IMAGE batch with one result per destination frame, collected in CPU memory.

**Example:** `background frames → destination`, `generated foreground frames → source`, `foreground masks → mask`, then `compositor.IMAGE → Create Video.images`. A single still source and/or single mask broadcasts across all destination frames.

**Limits:** source and mask must each contain exactly one frame or match the destination count. Other length combinations are rejected to prevent silent frame mismatches. Placement, resizing and mask semantics follow the core compositor. The helper does not synchronize independently timed videos or create audio. Complete output frames still require host RAM.

## Save with reference duration and original audio

**Node ID:** `PapanSaveReferenceVideo`

Saves a generated VIDEO up to the original reference's duration and copies the reference's first encoded audio track into MP4. Use it when LTX padding creates a longer result and you want to keep the source audio without an additional audio encode. For ordinary saving or changing codecs, built-in **Save Video** is preferable.

**Inputs:** `video` (generated VIDEO), `reference` (original file-backed VIDEO) and `filename_prefix` (STRING, default `Papan/video`, relative to ComfyUI's output directory). **No graph outputs**; the node saves an MP4 and displays its result.

**Example:** `generated frames → Create Video.images`, set that node's frame rate to the generation rate, then `Create Video.VIDEO → saver.video` and `Papan Board original VIDEO → saver.reference`. Do not connect a silent pose derivative as the audio reference. An output name resembles `Papan/video_00001_.mp4`.

**Limits and storage behavior:**

- The generated VIDEO must be at least as long as the reference. It is encoded to a temporary H.264 file with lossless CRF 0 and the ultrafast preset, then remuxed. This is a video encode, not an original generated-video packet copy. Temporary lossless files can be large and slower than the built-in saver.
- The reference must be an untrimmed file-backed VIDEO whose first video stream starts at timestamp zero. Save a trimmed/virtual/nonzero-start reference to a new file first. This restriction prevents copying audio from outside a virtual trim.
- Only the first reference audio track is copied. It must be MP4-compatible, such as AAC. Other audio codecs can cause the save to fail; nothing automatically installs FFmpeg or transcodes that audio. A silent reference produces a silent result; audio attached to the generated VIDEO is ignored.
- Video packets at or after the reference end are removed. Audio packets starting at or after that end are removed; an audio packet straddling the end is retained intact. The output can overrun by one audio packet. Frame/packet granularity and codec priming prevent a sample-exact duration guarantee. Audio packet payloads are preserved; this is not a promise of bit-identical container bytes.
- Workflow/prompt metadata and inherited stream metadata are deliberately omitted. Save the workflow separately in ComfyUI. This avoids embedding Papan session state, private paths or other node inputs in a shared video. The earlier local saver's metadata behavior is not retained.
- Files stay inside ComfyUI's output directory; existing results are not overwritten. Work is staged in ComfyUI's temporary directory. Temporary files and a newly created incomplete output are cleaned after errors. This is a normal plaintext output: locking a Papan board cannot retract previously generated files.

## Example sequence

```text
Papan Board original VIDEO ──┬──> Load motion driving video ──> IMAGE frames
                            ├──> Full reference timing.reference
                            └──> Save with reference duration.reference

IMAGE frames ──> Full reference timing.images ──> your LTX/motion pipeline
IMAGE frames ──> Automatic video foreground masks ──> optional compositor.mask
target IMAGE + first reference frame ──> Match target framing ──> generator target
generated frames + background frames + masks ──> optional Composite video frames
final frames ──> Create Video (same fps) ──> Save with reference duration.video
```

This is a wiring guide, not a ready-to-run generation workflow. Generator/model inputs differ, and no private workflow, media, model file or third-party asset is included.

## Provenance and verification

The earlier standalone local file had no license or upstream attribution header, so it is not treated as a third-party source distribution. This implementation was written here against the public APIs below, preserving the six local node IDs and their input/output names. It contains no vendored ComfyUI/VHS implementation, model weights or assets. The new source carries the repository's GPL-3.0 license identifier; upstream dependencies and models retain their own licenses.

API sources inspected: ComfyUI [VIDEO implementation](https://github.com/Comfy-Org/ComfyUI/blob/b0f4b7b294ce482a2e071d9d762c133d38c7aa07/comfy_api/latest/_input_impl/video_types.py), [core compositor](https://github.com/Comfy-Org/ComfyUI/blob/b0f4b7b294ce482a2e071d9d762c133d38c7aa07/comfy_extras/nodes_mask.py), [requirements](https://github.com/Comfy-Org/ComfyUI/blob/b0f4b7b294ce482a2e071d9d762c133d38c7aa07/requirements.txt) and [license](https://github.com/Comfy-Org/ComfyUI/blob/b0f4b7b294ce482a2e071d9d762c133d38c7aa07/LICENSE); VideoHelperSuite's [public loader](https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite/blob/4d907bee61e92c2e65af3bd6383a4e4d356126d1/videohelpersuite/load_video_nodes.py) and [license](https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite/blob/4d907bee61e92c2e65af3bd6383a4e4d356126d1/LICENSE). Both inspected upstream projects carry GPL-3.0 licenses. The adapters call those APIs rather than copying their implementations.

Run the focused synthetic suite with an existing Python environment containing CPU-capable PyTorch, SciPy and PyAV:

```sh
CUDA_VISIBLE_DEVICES='' python -m unittest discover -s optional_nodes/tests -v
```

It checks registration, missing VHS and provider-schema isolation, VIDEO trim rejection, padding, bounded batch order/broadcasts, first-frame geometry, actual synthetic H.264/AAC saves, audio packet preservation/end bounds, silent references, omitted metadata, output boundaries, filename collisions and failure cleanup. API providers are stubbed in the unit suite; numerical tensors and PyAV encoding/muxing are real. It downloads no models and performs no generation. The separate helper CI job installs CPU PyTorch only for tests, leaving the base extension check and requirements unchanged.

For an additional CPU smoke against existing installed API implementations, run with ComfyUI's Python and explicitly select its source directory. `PAPAN_VHS_DIR` is optional and selects an existing VideoHelperSuite directory:

```sh
PAPAN_COMFYUI_DIR=/path/to/ComfyUI \
PAPAN_VHS_DIR=/path/to/ComfyUI/custom_nodes/ComfyUI-VideoHelperSuite \
python optional_nodes/tests/native_api_smoke.py
```

This creates and removes only synthetic media in a disposable temporary directory. It starts no ComfyUI server, uses CPU mode, disables bytecode writes to installed sources, and loads no model. It checks real VIDEO encoding/saving, audio packet copying, the core compositor and (when selected) actual VHS frame decoding. VHS's unused server queue is stubbed because no server is running.

Local Linux CPU checks pass on ComfyUI `b0f4b7b294ce482a2e071d9d762c133d38c7aa07` / Python 3.12.12 / PyTorch 2.13.0 / PyAV 18.0.0 and [ComfyUI `8713616fcc321624e306bbd36be5906203dcb0dc`](https://github.com/Comfy-Org/ComfyUI/commit/8713616fcc321624e306bbd36be5906203dcb0dc) / Python 3.13.12 / PyTorch 2.12.1 / PyAV 18.1.0, both with VHS `4d907bee61e92c2e65af3bd6383a4e4d356126d1`.

Full model inference, motion quality, GPU/VRAM budgets, long-video throughput, all codecs/timestamps, Windows/macOS UI and a complete generated workflow remain unverified. Installed personal adapters and the running ComfyUI process are not updated by this PR. No saved board format, library protocol, reference-slot identity or Papan Board runtime changes.
