import asyncio
import hashlib
import json
import math
from pathlib import Path
from uuid import UUID

import folder_paths
import nodes
from comfy_api.latest import InputImpl

from .board import get_session, session_operation


class BoardOutputs(tuple):
    def __getitem__(self, index):
        return "*" if isinstance(index, int) and index >= 0 else super().__getitem__(index)


def references(media_state):
    entries = json.loads(media_state)
    if not isinstance(entries, list):
        raise ValueError("Invalid Papan board previews.")
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("kind") not in ("image", "video", "seconds") or not isinstance(entry.get("title"), str):
            raise ValueError("Invalid Papan reference.")
        if entry["kind"] == "seconds":
            source = entry.get("source_slot")
            if type(source) is not int or not 0 <= source < len(entries) or not isinstance(entries[source], dict) or entries[source].get("kind") != "video":
                raise ValueError("A Papan duration must reference a video on this node.")
            continue
        if entry.get("filename") is not None:
            reference_file(entry["filename"])
        else:
            UUID(entry["pinId"])
            UUID(entry["itemId"])
    return entries


def reference_file(filename):
    if not isinstance(filename, str) or Path(filename).is_absolute():
        raise ValueError("Invalid Papan reference filename.")
    root = Path(folder_paths.get_input_directory()).resolve()
    file = (root / filename).resolve()
    if not file.is_relative_to(root):
        raise ValueError("Papan references must stay inside ComfyUI's input directory.")
    return file


def connected_slots(entries, prompt, unique_id):
    if prompt is None:
        return list(range(len(entries)))
    slots = set()
    for node in prompt.values():
        for value in node.get("inputs", {}).values():
            if isinstance(value, list) and len(value) == 2 and str(value[0]) == str(unique_id):
                index = value[1]
                if not isinstance(index, int) or not 0 <= index < len(entries):
                    raise ValueError("A Papan connection points to a missing preview. Reconnect it on the board node.")
                slots.add(index)
    return sorted(slots)


class PapanReferences:
    CATEGORY = "Papan"
    FUNCTION = "load"
    # ComfyUI indexes this tuple when validating links. Actual output count follows the board.
    RETURN_TYPES = BoardOutputs(("*",))
    DESCRIPTION = "Open a Papan file and select previews to connect as IMAGE or VIDEO. Videos also output their duration in seconds as FLOAT."

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"media_state": ("STRING", {"default": "[]", "multiline": False})},
                "optional": {"session_tokens": ("STRING", {"default": "{}", "hidden": True})},
                "hidden": {"prompt": "PROMPT", "unique_id": "UNIQUE_ID"}}

    @classmethod
    def VALIDATE_INPUTS(cls, media_state, session_tokens="{}"):
        try:
            references(media_state)
            if not isinstance(json.loads(session_tokens), dict):
                raise ValueError("Invalid Papan board session.")
            return True
        except (ValueError, OSError, KeyError, TypeError) as error:
            return str(error)

    @classmethod
    def IS_CHANGED(cls, media_state, session_tokens="{}", prompt=None, unique_id=None):
        entries = references(media_state)
        digest = hashlib.sha256(media_state.encode())
        tokens = json.loads(session_tokens)
        slots = connected_slots(entries, prompt, unique_id)
        for index in sorted({entries[slot]["source_slot"] if entries[slot]["kind"] == "seconds" else slot for slot in slots}):
            entry = entries[index]
            if entry.get("filename") and reference_file(entry["filename"]).is_file():
                with reference_file(entry["filename"]).open("rb") as stream:
                    digest.update(hashlib.file_digest(stream, "sha256").digest())
            else:
                session = get_session(tokens.get(entry.get("boardId"), ""))
                digest.update(str(session["board"].file.stat().st_mtime_ns).encode())
        return digest.hexdigest()

    async def load(self, media_state, session_tokens="{}", prompt=None, unique_id=None):
        entries = references(media_state)
        tokens = json.loads(session_tokens)
        output, saved, loaded, durations = [None] * len(entries), [], {}, {}
        for index in connected_slots(entries, prompt, unique_id):
            source = entries[index]["source_slot"] if entries[index]["kind"] == "seconds" else index
            if source not in loaded:
                entry = dict(entries[source])
                if not entry.get("filename") or not reference_file(entry["filename"]).is_file():
                    session = get_session(tokens.get(entry.get("boardId"), ""))
                    result = await asyncio.to_thread(session_operation, session, session["board"].import_media, entry["pinId"], entry["itemId"], folder_paths.get_input_directory())
                    entry["filename"] = result["subfolder"] + "/" + result["name"]
                    saved.append({"slot": source, **entry})
                loaded[source] = nodes.LoadImage().load_image(entry["filename"])[0] if entry["kind"] == "image" else InputImpl.VideoFromFile(str(reference_file(entry["filename"])))
            if entries[index]["kind"] == "seconds":
                seconds = loaded[source].get_duration()
                if not math.isfinite(seconds) or seconds <= 0:
                    raise ValueError("This video has no readable duration. Restore it in Papan or choose another clip.")
                output[index] = float(seconds)
                durations[source] = seconds
            else:
                output[index] = loaded[source]
        return {"result": tuple(output), "ui": {"papan_imported": saved, "papan_durations": [{"slot": slot, "seconds": seconds} for slot, seconds in durations.items()]}}
