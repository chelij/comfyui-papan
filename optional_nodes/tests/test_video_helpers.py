import copy
from fractions import Fraction
import importlib.util
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import av
import numpy as np
import torch


ROOT = Path(__file__).resolve().parents[2]
nodes = types.ModuleType("nodes")
nodes.NODE_CLASS_MAPPINGS = {}
nodes.MAX_RESOLUTION = 16384
folder_paths = types.ModuleType("folder_paths")
comfy = types.ModuleType("comfy")
comfy.utils = types.ModuleType("comfy.utils")
comfy.utils.common_upscale = lambda image, width, height, method, crop: torch.nn.functional.interpolate(
    image, size=(height, width), mode=method, align_corners=False)
spec = importlib.util.spec_from_file_location("papan_video_helpers", ROOT / "optional_nodes/papan_motion_video.py")
helpers = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"nodes": nodes, "folder_paths": folder_paths, "comfy": comfy, "comfy.utils": comfy.utils}):
    spec.loader.exec_module(helpers)


class Video:
    def __init__(self, path=None, duration=1, trim=(0, 0)):
        self.path, self.duration, self.trim = path, duration, trim

    def get_duration(self):
        return self.duration

    def get_stream_source(self):
        return str(self.path) if self.path else None

    def get_active_trim_window(self):
        return self.trim

    def save_to(self, destination, **kwargs):
        self.saved_options = kwargs
        shutil.copyfile(self.path, destination)


def encode_fixture(path, seconds=1, audio_seconds=0, start_frame=0):
    with av.open(str(path), "w", options={"movflags": "use_metadata_tags"}) as output:
        output.metadata["private_test_marker"] = "synthetic-secret"
        stream = output.add_stream("libx264", rate=24)
        stream.width = stream.height = 16
        stream.pix_fmt = "yuv420p"
        stream.options = {"preset": "ultrafast", "crf": "0"}
        if audio_seconds:
            audio = output.add_stream("aac", rate=48000)
            audio.layout = "mono"
        for i in range(round(seconds * 24)):
            frame = av.VideoFrame.from_ndarray(np.full((16, 16, 3), i % 255, dtype=np.uint8), format="rgb24")
            frame.pts, frame.time_base = i + start_frame, Fraction(1, 24)
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
        if audio_seconds:
            for offset in range(0, round(audio_seconds * 48000), 1024):
                samples = np.sin(np.arange(offset, offset + 1024, dtype=np.float32) * (440 * 2 * np.pi / 48000))[None] * 0.2
                frame = av.AudioFrame.from_ndarray(samples, format="fltp", layout="mono")
                frame.sample_rate = 48000
                frame.pts, frame.time_base = offset, Fraction(1, 48000)
                for packet in audio.encode(frame):
                    output.mux(packet)
            for packet in audio.encode():
                output.mux(packet)


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        nodes.NODE_CLASS_MAPPINGS.clear()

    def test_registration_and_optional_installation(self):
        expected = {"PapanMotionVideoLoader", "PapanMotionFrameTiming", "PapanVideoForegroundMask",
                    "PapanMatchReferenceFraming", "PapanVideoCompositeMasked", "PapanSaveReferenceVideo"}
        self.assertEqual(set(helpers.NODE_CLASS_MAPPINGS), expected)
        self.assertEqual(set(helpers.NODE_DISPLAY_NAME_MAPPINGS), expected)
        self.assertTrue(all(node.CATEGORY == "Papan/Video helpers" for node in helpers.NODE_CLASS_MAPPINGS.values()))
        self.assertNotIn("papan_motion_video", (ROOT / "__init__.py").read_text())
        self.assertEqual((ROOT / "requirements.txt").read_text().strip(), "cryptography>=41")

    def test_loader_without_vhs_discovers_and_explains_dependency(self):
        schema = helpers.PapanMotionVideoLoader.INPUT_TYPES()
        self.assertEqual(next(iter(schema["required"])), "video")
        self.assertEqual(schema["required"]["video"], ("VIDEO",))
        with self.assertRaisesRegex(ValueError, "Install VideoHelperSuite"):
            helpers.PapanMotionVideoLoader().load_video(Video())

    def test_loader_preserves_provider_schema_and_uses_its_public_loader(self):
        schema = {"required": {"video": ("STRING",), "force_rate": ("FLOAT", {"default": 0}),
                               "custom_width": ("INT", {"default": 0}), "frame_load_cap": ("INT", {"default": 50})},
                  "optional": {"format": (["None", "other"], {"default": "other"}), "meta_batch": ("BATCH",), "vae": ("VAE",)},
                  "hidden": {"unique_id": "UNIQUE_ID"}}
        original = copy.deepcopy(schema)
        calls = []
        class Loader:
            INPUT_TYPES = staticmethod(lambda: schema)
            def load_video(self, **kwargs):
                calls.append(kwargs)
                return "frames", 5, "audio", "info"
        nodes.NODE_CLASS_MAPPINGS["VHS_LoadVideoPath"] = Loader
        adjusted = helpers.PapanMotionVideoLoader.INPUT_TYPES()
        self.assertEqual(schema, original)
        self.assertEqual(adjusted["required"]["force_rate"][1]["default"], 30)
        self.assertNotIn("vae", adjusted["optional"])
        self.assertEqual(adjusted["hidden"], schema["hidden"])
        source = self.root / "source.mp4"
        source.touch()
        self.assertEqual(helpers.PapanMotionVideoLoader().load_video(Video(source), force_rate=24)[1], 5)
        self.assertEqual(calls, [{"video": str(source), "force_rate": 24}])
        for video in [Video(), Video(source, trim=(1, 0)), Video(source, trim=(0, 1))]:
            with self.assertRaises(ValueError):
                helpers.PapanMotionVideoLoader().load_video(video)

    def test_timing_keeps_all_frames_and_only_repeats_last_frame(self):
        images = torch.arange(17, dtype=torch.float32).reshape(17, 1, 1, 1)
        padded, duration = helpers.PapanMotionFrameTiming().pad_frames(images, Video(duration=0.75), 24)
        self.assertEqual(duration, 0.75)
        self.assertEqual(len(padded), 25)
        torch.testing.assert_close(padded[:17], images)
        self.assertEqual(padded[17:].flatten().tolist(), [16] * 8)
        images = images[:9]
        self.assertIs(helpers.PapanMotionFrameTiming().pad_frames(images, Video(duration=9/24), 24)[0], images)
        for frames, reference, fps in [(images[:0], Video(), 24), (images, Video(duration=0), 24), (images, Video(), float("nan"))]:
            with self.assertRaises(ValueError):
                helpers.PapanMotionFrameTiming().pad_frames(frames, reference, fps)

    def test_masks_are_ordered_cpu_outputs_and_use_bounded_batches(self):
        images = torch.arange(7, dtype=torch.float32).reshape(7, 1, 1, 1).expand(-1, 2, 3, 3).clone()
        calls = []
        class Model:
            def encode_image(self, image):
                calls.append(len(image))
                return image[..., 0]
        masks, = helpers.PapanVideoForegroundMask().mask_frames(Model(), images, 3)
        self.assertEqual(calls, [3, 3, 1])
        torch.testing.assert_close(masks, images[..., 0])
        self.assertEqual(masks.device.type, "cpu")
        for frames, size in [(images[:0], 3), (images, 0)]:
            with self.assertRaises(ValueError):
                helpers.PapanVideoForegroundMask().mask_frames(Model(), frames, size)

    def test_composite_keeps_pairing_broadcasts_and_checks_lengths(self):
        calls = []
        class Composite:
            @classmethod
            def execute(cls, destination, source, x, y, resize_source, mask):
                calls.append((len(destination), len(source), len(mask) if mask is not None else None, x, y, resize_source))
                return types.SimpleNamespace(result=(destination + source * (mask[..., None] if mask is not None else 1),))
        nodes.NODE_CLASS_MAPPINGS["ImageCompositeMasked"] = Composite
        destination = torch.arange(5, dtype=torch.float32).reshape(5, 1, 1, 1).expand(-1, 2, 2, 3).clone()
        source = torch.ones_like(destination) * 10
        mask = torch.arange(5, dtype=torch.float32).reshape(5, 1, 1).expand(-1, 2, 2) / 4
        output, = helpers.PapanVideoCompositeMasked().composite(destination, source, 3, 4, True, 2, mask)
        torch.testing.assert_close(output, destination + source * mask[..., None])
        self.assertEqual(calls, [(2, 2, 2, 3, 4, True), (2, 2, 2, 3, 4, True), (1, 1, 1, 3, 4, True)])
        self.assertEqual(output.device.type, "cpu")
        output, = helpers.PapanVideoCompositeMasked().composite(destination, source[:1], 0, 0, False, 2, mask[:1])
        torch.testing.assert_close(output, destination)
        for src, masks in [(source[:2], None), (source, mask[:2])]:
            with self.assertRaisesRegex(ValueError, "match"):
                helpers.PapanVideoCompositeMasked().composite(destination, src, 0, 0, False, 2, masks)

    def test_framing_selects_largest_component_and_places_on_white(self):
        target = torch.zeros((1, 6, 6, 3))
        target[:, 1:5, 2:4, 1] = 1
        reference = torch.zeros((2, 10, 10, 3))
        reference_mask = torch.zeros((2, 10, 10))
        reference_mask[:, 2:8, 4:7] = 1
        reference_mask[:, 0, 0] = 1
        model = types.SimpleNamespace(encode_image=lambda image: image[..., 1])
        output, = helpers.PapanMatchReferenceFraming().match_framing(target, reference, model, 0.5, 1, reference_mask)
        expected = torch.ones((1, 10, 10, 3))
        expected[:, 2:8, 4:7] = torch.tensor([0., 1., 0.])
        torch.testing.assert_close(output, expected)
        with self.assertRaisesRegex(ValueError, "No foreground"):
            helpers.PapanMatchReferenceFraming().match_framing(target * 0, reference, model, 0.5, 1, reference_mask)
        with self.assertRaisesRegex(ValueError, "dimensions"):
            helpers.PapanMatchReferenceFraming().match_framing(target, reference, model, 0.5, 1, reference_mask[:, :2])


class SaveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "output"
        self.output.mkdir()
        self.scratch = self.root / "temp"
        self.scratch.mkdir()
        folder_paths.get_output_directory = lambda: str(self.output)
        folder_paths.get_temp_directory = lambda: str(self.scratch)
        folder_paths.get_save_image_path = lambda prefix, root: (str(self.output), prefix, 1, "", prefix)
        self.generated, self.reference = self.root / "generated.mp4", self.root / "reference.mp4"
        encode_fixture(self.generated, seconds=1.5)
        encode_fixture(self.reference, seconds=1, audio_seconds=2)
        self.video = Video(self.generated, duration=1.5)
        self.ref = Video(self.reference, duration=1)

    def test_save_trims_video_and_audio_preserves_audio_packets_and_omits_metadata(self):
        result = helpers.PapanSaveReferenceVideo().save_video(self.video, self.ref, "clip", prompt={"secret": "sentinel"})
        path = self.output / result["ui"]["images"][0]["filename"]
        with av.open(str(path)) as saved:
            self.assertEqual(len(list(saved.decode(video=0))), 24)
            self.assertNotIn("private_test_marker", saved.metadata)
            self.assertNotIn("prompt", saved.metadata)
            self.assertLessEqual(saved.duration / av.time_base, 1 + 1024/48000 + 0.001)
        with av.open(str(self.reference)) as source, av.open(str(path)) as saved:
            original = [bytes(p) for p in source.demux(audio=0) if p.pts is not None and p.pts * p.time_base < 1]
            copied = [bytes(p) for p in saved.demux(audio=0) if p.pts is not None]
            self.assertEqual(copied, original)
        self.assertEqual(self.video.saved_options, {"codec": "h264", "crf": 0, "preset": "ultrafast"})
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_silent_reference_stays_silent(self):
        result = helpers.PapanSaveReferenceVideo().save_video(self.video, Video(self.generated, duration=1), "silent")
        with av.open(str(self.output / result["ui"]["images"][0]["filename"])) as saved:
            self.assertFalse(saved.streams.audio)

    def test_refuses_short_trimmed_and_outside_output_without_writes(self):
        for video, reference, prefix in [(Video(self.generated, duration=0.5), self.ref, "short"),
                                         (self.video, Video(self.reference, trim=(0.5, 0)), "trimmed"),
                                         (self.video, self.ref, "../outside")]:
            with self.assertRaises(ValueError):
                helpers.PapanSaveReferenceVideo().save_video(video, reference, prefix)
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_existing_output_and_copy_failure_do_not_destroy_files(self):
        existing = self.output / "existing_00001_.mp4"
        existing.write_bytes(b"keep this result")
        with self.assertRaisesRegex(ValueError, "already exists"):
            helpers.PapanSaveReferenceVideo().save_video(self.video, self.ref, "existing")
        self.assertEqual(existing.read_bytes(), b"keep this result")
        with patch.object(helpers.shutil, "copyfileobj", side_effect=OSError("synthetic disk failure")):
            with self.assertRaisesRegex(OSError, "disk failure"):
                helpers.PapanSaveReferenceVideo().save_video(self.video, self.ref, "failed")
        self.assertFalse((self.output / "failed_00001_.mp4").exists())
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_encoder_failure_cleans_staging_and_nonzero_reference_is_rejected(self):
        with patch.object(self.video, "save_to", side_effect=OSError("synthetic encoder failure")):
            with self.assertRaisesRegex(OSError, "encoder failure"):
                helpers.PapanSaveReferenceVideo().save_video(self.video, self.ref, "failed")
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual(list(self.scratch.iterdir()), [])
        shifted = self.root / "shifted.mp4"
        encode_fixture(shifted, start_frame=24)
        with av.open(str(shifted)) as source:
            self.assertGreater(source.streams.video[0].start_time, 0)
        with self.assertRaisesRegex(ValueError, "timestamp zero"):
            helpers.PapanSaveReferenceVideo().save_video(self.video, Video(shifted), "shifted")
        self.assertEqual(list(self.output.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
