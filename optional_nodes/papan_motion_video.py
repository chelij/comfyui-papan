# SPDX-License-Identifier: GPL-3.0-only
"""Optional standalone nodes. Install this file once; see docs/video-helpers.md."""

import copy
from fractions import Fraction
import heapq
import math
from pathlib import Path
import shutil
import tempfile

import av
import torch
from scipy import ndimage

import comfy.utils
import folder_paths
import nodes


def file_source(video):
    source = video.get_stream_source()
    if not isinstance(source, str) or not Path(source).is_file():
        raise ValueError("Use a file-backed VIDEO; save an in-memory video first.")
    trim_window = getattr(video, "get_active_trim_window", None)
    if trim_window is not None and any(trim_window()):
        raise ValueError("Save the trimmed VIDEO to a new file first; its raw source includes the untrimmed clip.")
    return source


def reference_duration(reference):
    duration = reference.get_duration()
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("The reference VIDEO must have a finite, positive duration.")
    return duration


def batch_ranges(images, batch_size):
    if len(images) == 0 or not isinstance(batch_size, int) or batch_size < 1:
        raise ValueError("Provide at least one frame and a positive integer batch_size.")
    return ((start, min(start + batch_size, len(images))) for start in range(0, len(images), batch_size))


class PapanMotionVideoLoader:
    CATEGORY = "Papan/Video helpers"
    FUNCTION = "load_video"
    RETURN_TYPES = ("IMAGE", "INT", "AUDIO", "VHS_VIDEOINFO")
    RETURN_NAMES = ("IMAGE", "frame_count", "audio", "video_info")
    DESCRIPTION = "Load a file-backed VIDEO as frames using VideoHelperSuite. Requires VHS_LoadVideoPath."

    @classmethod
    def INPUT_TYPES(cls):
        loader = nodes.NODE_CLASS_MAPPINGS.get("VHS_LoadVideoPath")
        if loader is None:
            inputs = {"required": {
                "force_rate": ("FLOAT", {"default": 30, "min": 0, "max": 60}),
                "custom_width": ("INT", {"default": 640, "min": 0, "max": nodes.MAX_RESOLUTION}),
                "custom_height": ("INT", {"default": 0, "min": 0, "max": nodes.MAX_RESOLUTION}),
                "frame_load_cap": ("INT", {"default": 0, "min": 0}),
                "skip_first_frames": ("INT", {"default": 0, "min": 0}),
                "select_every_nth": ("INT", {"default": 1, "min": 1}),
            }, "optional": {"format": (["None"],)}}
        else:
            inputs = copy.deepcopy(loader.INPUT_TYPES())
            inputs["required"]["force_rate"][1]["default"] = 30
            inputs["required"]["custom_width"][1]["default"] = 640
            inputs["required"]["frame_load_cap"][1]["default"] = 0
            inputs.get("optional", {}).pop("meta_batch", None)
            inputs.get("optional", {}).pop("vae", None)
            inputs["optional"]["format"][1]["default"] = "None"
        inputs["required"] = {"video": ("VIDEO",), **{k: v for k, v in inputs["required"].items() if k != "video"}}
        return inputs

    def load_video(self, video, **kwargs):
        loader = nodes.NODE_CLASS_MAPPINGS.get("VHS_LoadVideoPath")
        if loader is None:
            raise ValueError("Install VideoHelperSuite to use Load motion driving video (Papan).")
        return loader().load_video(video=file_source(video), **kwargs)


class PapanMotionFrameTiming:
    CATEGORY = "Papan/Video helpers"
    FUNCTION = "pad_frames"
    RETURN_TYPES = ("IMAGE", "FLOAT")
    RETURN_NAMES = ("padded_motion", "reference_duration")
    DESCRIPTION = "Repeat the last frame to reach the reference duration and an LTX 8n+1 frame count. Use the actual frame rate."

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"images": ("IMAGE",), "reference": ("VIDEO",),
                             "fps": ("FLOAT", {"default": 30, "min": 1, "max": 240})}}

    def pad_frames(self, images, reference, fps):
        if len(images) == 0 or not math.isfinite(fps) or fps <= 0:
            raise ValueError("Provide at least one frame and a finite, positive fps.")
        duration = reference_duration(reference)
        count = max(len(images), math.ceil(duration * fps))
        count += (1 - count) % 8
        if count > len(images):
            images = torch.cat([images, images[-1:].expand(count - len(images), -1, -1, -1)])
        return images, duration


class PapanVideoForegroundMask:
    CATEGORY = "Papan/Video helpers"
    FUNCTION = "mask_frames"
    RETURN_TYPES = ("MASK",)
    DESCRIPTION = "Run a BACKGROUND_REMOVAL model in small batches and collect masks in CPU memory."

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"bg_removal_model": ("BACKGROUND_REMOVAL",), "image": ("IMAGE",),
                             "batch_size": ("INT", {"default": 8, "min": 1, "max": 64})}}

    def mask_frames(self, bg_removal_model, image, batch_size):
        output = None
        for start, end in batch_ranges(image, batch_size):
            masks = bg_removal_model.encode_image(image[start:end]).to("cpu")
            if output is None:
                output = torch.empty((len(image), *masks.shape[1:]), dtype=masks.dtype, device="cpu")
            output[start:end] = masks
        return (output,)


class PapanMatchReferenceFraming:
    CATEGORY = "Papan/Video helpers"
    FUNCTION = "match_framing"
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("aligned_target",)
    DESCRIPTION = "Place the first target subject on white, matching the largest subject in the first reference frame. No tracking or motion transfer."

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"target_image": ("IMAGE",), "reference_image": ("IMAGE",),
                             "bg_removal_model": ("BACKGROUND_REMOVAL",),
                             "mask_threshold": ("FLOAT", {"default": 0.5, "min": 0.01, "max": 0.99, "step": 0.01}),
                             "size_multiplier": ("FLOAT", {"default": 1.0, "min": 0.25, "max": 2.0, "step": 0.01})},
                "optional": {"reference_mask": ("MASK",)}}

    @staticmethod
    def foreground_box(mask, threshold, label):
        binary = mask.detach().cpu().numpy() > threshold
        labels, count = ndimage.label(binary, structure=ndimage.generate_binary_structure(2, 2))
        if not count:
            raise ValueError(f"No foreground in {label}; adjust the mask or threshold.")
        areas = ndimage.sum_labels(binary, labels, range(1, count + 1))
        rows, columns = ndimage.find_objects(labels)[int(areas.argmax())]
        return columns.start, rows.start, columns.stop - columns.start, rows.stop - rows.start

    def match_framing(self, target_image, reference_image, bg_removal_model, mask_threshold, size_multiplier, reference_mask=None):
        if len(target_image) == 0 or len(reference_image) == 0:
            raise ValueError("Provide a target image and at least one reference frame.")
        if not math.isfinite(size_multiplier) or size_multiplier <= 0:
            raise ValueError("size_multiplier must be finite and positive.")
        target = target_image[:1, :, :, :3]
        reference = reference_image[:1, :, :, :3]
        if reference_mask is None:
            reference_mask = bg_removal_model.encode_image(reference)
        if len(reference_mask) == 0 or reference_mask.shape[-2:] != reference.shape[1:3]:
            raise ValueError("reference_mask must match the reference frame dimensions.")
        rx, ry, rw, rh = self.foreground_box(reference_mask[0], mask_threshold, "the first reference frame")
        target_mask = bg_removal_model.encode_image(target).clamp(0, 1).to(target)
        tx, ty, tw, th = self.foreground_box(target_mask[0], mask_threshold, "the target image")
        height, width = reference.shape[1:3]
        scale = min(rh * size_multiplier / th, (height - ry) / th)
        scaled_width, scaled_height = max(1, round(tw * scale)), max(1, round(th * scale))
        left = round(rx + rw / 2 - scaled_width / 2)
        color = target[:, ty:ty + th, tx:tx + tw].movedim(-1, 1)
        alpha = target_mask[:, ty:ty + th, tx:tx + tw].unsqueeze(1)
        color = comfy.utils.common_upscale(color, scaled_width, scaled_height, "bicubic", "disabled").movedim(1, -1).clamp(0, 1)
        alpha = comfy.utils.common_upscale(alpha, scaled_width, scaled_height, "bilinear", "disabled").movedim(1, -1).clamp(0, 1)
        canvas = torch.ones((1, height, width, 3), device=target.device, dtype=target.dtype)
        x0, x1 = max(left, 0), min(left + scaled_width, width)
        canvas[:, ry:ry + scaled_height, x0:x1] = (color * alpha + 1 - alpha)[:, :, x0 - left:x1 - left]
        return (canvas,)


class PapanVideoCompositeMasked:
    CATEGORY = "Papan/Video helpers"
    FUNCTION = "composite"
    RETURN_TYPES = ("IMAGE",)
    DESCRIPTION = "Call ComfyUI ImageCompositeMasked in batches and collect the full result in CPU memory."

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"destination": ("IMAGE",), "source": ("IMAGE",),
                             "x": ("INT", {"default": 0, "min": 0, "max": nodes.MAX_RESOLUTION}),
                             "y": ("INT", {"default": 0, "min": 0, "max": nodes.MAX_RESOLUTION}),
                             "resize_source": ("BOOLEAN", {"default": False}),
                             "batch_size": ("INT", {"default": 8, "min": 1, "max": 64})},
                "optional": {"mask": ("MASK",)}}

    def composite(self, destination, source, x, y, resize_source, batch_size, mask=None):
        if len(source) not in (1, len(destination)) or (mask is not None and len(mask) not in (1, len(destination))):
            raise ValueError("Source and mask must have one frame or match the destination frame count.")
        compositor = nodes.NODE_CLASS_MAPPINGS["ImageCompositeMasked"]
        output = None
        for start, end in batch_ranges(destination, batch_size):
            images = source if len(source) == 1 else source[start:end]
            masks = mask if mask is None or len(mask) == 1 else mask[start:end]
            result = compositor.execute(destination[start:end], images, x, y, resize_source, masks).result[0].to("cpu")
            if output is None:
                output = torch.empty((len(destination), *result.shape[1:]), device="cpu", dtype=result.dtype)
            output[start:end] = result
        return (output,)


def trimmed_packets(container, stream, output_stream, duration):
    for packet in container.demux(stream):
        if packet.pts is None or packet.pts * packet.time_base >= duration:
            continue
        if packet.duration and (packet.pts + packet.duration) * packet.time_base < 0:
            continue
        if stream.type == "video" and packet.duration:
            packet.duration = min(packet.duration, max(1, round(duration / packet.time_base) - packet.pts))
        packet.stream = output_stream
        yield packet


def packet_time(packet):
    return (packet.dts if packet.dts is not None else packet.pts) * packet.time_base


class PapanSaveReferenceVideo:
    CATEGORY = "Papan/Video helpers"
    FUNCTION = "save_video"
    RETURN_TYPES = ()
    OUTPUT_NODE = True
    DESCRIPTION = "Save generated video up to the reference duration with its first original audio track. Requires an untrimmed zero-start file reference and MP4-compatible audio. Workflow metadata is omitted."

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"video": ("VIDEO",), "reference": ("VIDEO",),
                             "filename_prefix": ("STRING", {"default": "Papan/video"})}}

    def save_video(self, video, reference, filename_prefix, prompt=None, extra_pnginfo=None):
        source = file_source(reference)
        duration = Fraction(str(reference_duration(reference)))
        if reference_duration(video) + 0.001 < float(duration):
            raise ValueError("Generated VIDEO is shorter than the reference; generate enough frames first.")
        with av.open(source) as ref:
            if not ref.streams.video or ref.streams.video[0].start_time not in (None, 0):
                raise ValueError("Use an untrimmed reference file whose video starts at timestamp zero.")
            root = Path(folder_paths.get_output_directory()).resolve()
            folder, filename, counter, subfolder, _ = folder_paths.get_save_image_path(filename_prefix, str(root))
            output_path = Path(folder) / f"{filename}_{counter:05}_.mp4"
            if not output_path.resolve().is_relative_to(root):
                raise ValueError("filename_prefix must stay inside ComfyUI's output directory.")
            with tempfile.TemporaryDirectory(prefix="papan-video-", dir=folder_paths.get_temp_directory()) as temporary:
                generated = str(Path(temporary) / "generated.mp4")
                video.save_to(generated, codec="h264", crf=0, preset="ultrafast")
                pending = Path(temporary) / "result.mp4"
                with av.open(generated) as gen, av.open(str(pending), "w") as out:
                    source_video = gen.streams.video[0]
                    if source_video.start_time not in (None, 0) or source_video.codec_context.name != "h264" or source_video.codec_context.has_b_frames:
                        raise ValueError("VIDEO.save_to must produce zero-start H.264 without B-frames (ultrafast preset).")
                    output_video = out.add_stream_from_template(source_video)
                    output_video.metadata.clear()
                    streams = [trimmed_packets(gen, source_video, output_video, duration)]
                    if ref.streams.audio:
                        source_audio = ref.streams.audio[0]
                        output_audio = out.add_stream_from_template(source_audio)
                        output_audio.metadata.clear()
                        streams.append(trimmed_packets(ref, source_audio, output_audio, duration))
                    for packet in heapq.merge(*streams, key=packet_time):
                        out.mux(packet)
                # Exclusive creation prevents overwriting an existing result on counter races.
                try:
                    output = output_path.open("xb")
                except FileExistsError:
                    raise ValueError("Output filename already exists; run again for a new counter.") from None
                try:
                    with output, pending.open("rb") as result:
                        shutil.copyfileobj(result, output)
                except Exception:
                    output_path.unlink(missing_ok=True)
                    raise
        return {"ui": {"images": [{"filename": output_path.name, "subfolder": subfolder, "type": "output"}], "animated": [True]}}


NODE_CLASS_MAPPINGS = {
    "PapanMotionVideoLoader": PapanMotionVideoLoader,
    "PapanMotionFrameTiming": PapanMotionFrameTiming,
    "PapanVideoForegroundMask": PapanVideoForegroundMask,
    "PapanMatchReferenceFraming": PapanMatchReferenceFraming,
    "PapanVideoCompositeMasked": PapanVideoCompositeMasked,
    "PapanSaveReferenceVideo": PapanSaveReferenceVideo,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "PapanMotionVideoLoader": "Load motion driving video (Papan)",
    "PapanMotionFrameTiming": "Full reference timing (LTX padding)",
    "PapanVideoForegroundMask": "Automatic video foreground masks in batches",
    "PapanMatchReferenceFraming": "Match target framing to reference video",
    "PapanVideoCompositeMasked": "Composite video frames in batches",
    "PapanSaveReferenceVideo": "Save with reference duration and original audio",
}
