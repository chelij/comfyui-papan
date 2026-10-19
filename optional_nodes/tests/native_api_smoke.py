"""Opt-in CPU smoke using existing ComfyUI APIs and generated media, never a server."""

import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import types
from fractions import Fraction


sys.dont_write_bytecode = True
os.environ["CUDA_VISIBLE_DEVICES"] = ""
runtime = Path(os.environ["PAPAN_COMFYUI_DIR"]).resolve()
sys.path.insert(0, str(runtime))
import comfy.options

comfy.options.enable_args_parsing()
sys.argv = ["papan-helper-native-smoke", "--cpu"]
nodes = types.ModuleType("nodes")
nodes.MAX_RESOLUTION = 16384
nodes.NODE_CLASS_MAPPINGS = {}
sys.modules["nodes"] = nodes

import av
import folder_paths
import torch
from comfy_extras.nodes_mask import ImageCompositeMasked
from comfy_api.latest._input_impl.video_types import VideoFromComponents, VideoFromFile
from comfy_api.latest._util import VideoComponents


nodes.NODE_CLASS_MAPPINGS["ImageCompositeMasked"] = ImageCompositeMasked
root = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("papan_native_helpers", root / "optional_nodes/papan_motion_video.py")
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
with tempfile.TemporaryDirectory(prefix="papan-helper-native-") as temporary:
    directory = Path(temporary)
    (directory / "output").mkdir()
    (directory / "temp").mkdir()
    folder_paths.set_output_directory(str(directory / "output"))
    folder_paths.set_temp_directory(str(directory / "temp"))
    frames = torch.zeros((25, 16, 16, 3))
    frames[..., 1] = 0.5
    audio = {"sample_rate": 48000, "waveform": torch.sin(torch.arange(50000) * (440 * 2 * torch.pi / 48000)).reshape(1, 1, -1) * 0.2}
    original = VideoFromComponents(VideoComponents(images=frames, audio=audio, frame_rate=Fraction(24)))
    generated = VideoFromComponents(VideoComponents(images=torch.cat([frames, frames[:8]]), frame_rate=Fraction(24)))
    path = directory / "reference.mp4"
    original.save_to(str(path), codec="h264", crf=0, preset="ultrafast")
    reference = VideoFromFile(str(path))
    result = helpers.PapanSaveReferenceVideo().save_video(generated, reference, "synthetic")
    output = directory / "output" / result["ui"]["images"][0]["filename"]
    saved = VideoFromFile(str(output))
    assert abs(saved.get_duration() - reference.get_duration()) <= 1/24
    with av.open(str(output)) as container:
        assert 25 <= len(list(container.decode(video=0))) <= 26
        assert len(container.streams.audio) == 1
    with av.open(str(path)) as source, av.open(str(output)) as destination:
        expected_audio = [bytes(packet) for packet in source.demux(audio=0) if packet.pts is not None and packet.pts * packet.time_base < reference.get_duration()]
        copied_audio = [bytes(packet) for packet in destination.demux(audio=0) if packet.pts is not None]
        assert copied_audio == expected_audio
    composite, = helpers.PapanVideoCompositeMasked().composite(frames, torch.ones((1, 16, 16, 3)), 0, 0, False, 4, torch.ones((1, 16, 16)))
    torch.testing.assert_close(composite, torch.ones_like(frames))
    print("PASS: native VIDEO.save_to, VideoFromFile and V3 ImageCompositeMasked; 33 generated / 25 reference frames and exact AAC packet payloads.")
    vhs = os.environ.get("PAPAN_VHS_DIR")
    if vhs:
        sys.path.insert(0, vhs)
        server = types.ModuleType("server")
        server.PromptServer = types.SimpleNamespace(instance=types.SimpleNamespace(
            prompt_queue=types.SimpleNamespace(currently_running={})))
        sys.modules["server"] = server
        from videohelpersuite.load_video_nodes import LoadVideoPath
        nodes.NODE_CLASS_MAPPINGS["VHS_LoadVideoPath"] = LoadVideoPath
        schema = helpers.PapanMotionVideoLoader.INPUT_TYPES()
        assert schema["required"]["video"] == ("VIDEO",)
        loaded = helpers.PapanMotionVideoLoader().load_video(reference, force_rate=24, custom_width=16,
            custom_height=16, frame_load_cap=0, skip_first_frames=0, select_every_nth=1, format="None")
        assert loaded[1] == 25
        torch.testing.assert_close(loaded[0], frames, atol=0.02, rtol=0)
        print("PASS: native VideoHelperSuite path loader; all 25 synthetic frames.")
    print(f"CPU environment: Python {sys.version.split()[0]}, torch {torch.__version__}, PyAV {av.__version__}.")
