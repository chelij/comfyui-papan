import asyncio
import atexit
import hashlib
import os
from pathlib import Path
import secrets
import shutil
import tempfile
import threading
import time
import zipfile

from aiohttp import web
from cryptography.exceptions import InvalidTag
import folder_paths
from server import PromptServer

from .board import PapanBoard, sessions, SESSION_SECONDS, discard, get_session, session_operation
from .nodes import PapanReferences


NODE_CLASS_MAPPINGS = {"PapanReferences": PapanReferences}
NODE_DISPLAY_NAME_MAPPINGS = {"PapanReferences": "Papan Board"}
WEB_DIRECTORY = "./web"
MAX_UPLOAD = 512 * 1024 * 1024


@atexit.register
def close_sessions():
    for token in list(sessions):
        discard(token)


def session_for(request):
    try:
        return get_session(request.match_info["token"])
    except PermissionError as error:
        raise web.HTTPUnauthorized(text=str(error)) from error


async def respond(session, operation, *args):
    try:
        result = await asyncio.to_thread(session_operation, session, operation, *args)
        if isinstance(result, bytes):
            return web.Response(body=result, content_type="image/png", headers={"Cache-Control": "no-store"})
        return web.json_response(result, headers={"Cache-Control": "no-store"})
    except PermissionError as error:
        return web.json_response({"error": str(error)}, status=401)
    except LookupError as error:
        return web.json_response({"error": str(error)}, status=404)
    except (ValueError, OSError, KeyError, TypeError, InvalidTag, zipfile.BadZipFile) as error:
        message = "Encrypted media is damaged or has changed. Open the file again." if isinstance(error, InvalidTag) else str(error)
        return web.json_response({"error": message}, status=400)


@PromptServer.instance.routes.post("/papan/open")
async def open_board(request):
    file, owned, filename, source = None, False, "", None
    try:
        if request.content_type == "application/json":
            data = await request.json()
            value = data.get("path")
            if "input_file" in data:
                root = Path(folder_paths.get_input_directory()).resolve()
                value = data["input_file"]
                if not isinstance(value, str) or Path(value).is_absolute():
                    raise ValueError("Invalid saved Papan file.")
                saved = (root / value).resolve()
                if not saved.is_relative_to(root):
                    raise ValueError("Saved Papan files must stay inside ComfyUI's input directory.")
                value = str(saved)
                source = {"input_file": data["input_file"]}
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Enter the full path to a Papan file on the ComfyUI server.")
            file = Path(value.strip()).expanduser()
            if not file.is_absolute():
                raise ValueError("Use the full path to the Papan file.")
            filename = file.name
            source = source or {"path": str(file)}
        else:
            reader = await request.multipart()
            part = await reader.next()
            if part is None or part.name != "file" or not part.filename or not part.filename.lower().endswith((".papan", ".papan.zip")):
                raise ValueError("Choose a .papan or .papan.zip board file.")
            filename = part.filename
            suffix = ".papan.zip" if filename.lower().endswith(".zip") else ".papan"
            directory = Path(folder_paths.get_temp_directory())
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(prefix="papan-", suffix=suffix, dir=directory, delete=False) as stream:
                file, owned = Path(stream.name), True
                size = 0
                digest = hashlib.sha256()
                while chunk := await part.read_chunk(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD:
                        raise ValueError("Board upload exceeds 512 MiB. Open it using its server file path instead.")
                    await asyncio.to_thread(stream.write, chunk)
                    digest.update(chunk)
        for token, session in list(sessions.items()):
            if time.monotonic() - session["touched"] > SESSION_SECONDS and not session["lock"].locked():
                discard(token)
        if len(sessions) >= 64:
            raise ValueError("Too many open Papan files. Close a board before opening another.")
        board = await asyncio.to_thread(PapanBoard, file)
        if owned:
            root = Path(folder_paths.get_input_directory()).resolve()
            directory = root / "papan" / "boards"
            directory.mkdir(parents=True, exist_ok=True)
            destination = directory / f"{digest.hexdigest()}{suffix}"
            if not directory.resolve().is_relative_to(root) or destination.is_symlink():
                raise ValueError("Saved Papan files must stay inside ComfyUI's input directory.")
            if not destination.exists():
                with tempfile.NamedTemporaryFile(dir=directory, suffix=".tmp", delete=False) as stream:
                    temporary = Path(stream.name)
                try:
                    await asyncio.to_thread(shutil.copyfile, file, temporary)
                    os.replace(temporary, destination)
                finally:
                    temporary.unlink(missing_ok=True)
            source = {"input_file": destination.relative_to(root).as_posix()}
        token = secrets.token_urlsafe(32)
        sessions[token] = {"board": board, "owned": owned, "lock": threading.Lock(), "touched": time.monotonic()}
        return web.json_response({**board.summary(), "fileName": filename, "source": source, "token": token}, headers={"Cache-Control": "no-store"})
    except (ValueError, OSError, KeyError, TypeError, zipfile.BadZipFile) as error:
        if owned and file:
            file.unlink(missing_ok=True)
        return web.json_response({"error": str(error)}, status=400)
    except BaseException:
        if owned and file:
            file.unlink(missing_ok=True)
        raise


@PromptServer.instance.routes.post("/papan/unlock/{token}")
async def unlock_board(request):
    session = session_for(request)
    data = await request.json()
    return await respond(session, session["board"].unlock, data.get("password"))


@PromptServer.instance.routes.delete("/papan/board/{token}")
async def close_board(request):
    session = session_for(request)
    await asyncio.to_thread(session_operation, session, discard, request.match_info["token"])
    return web.json_response({"closed": True})


@PromptServer.instance.routes.get("/papan/pins/{token}")
async def pins(request):
    session = session_for(request)
    try:
        offset = int(request.query.get("offset", "0"))
        limit = int(request.query.get("limit", "40"))
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError()
    except ValueError:
        return web.json_response({"error": "Use a nonnegative offset and a limit from 1 to 100."}, status=400)
    return await respond(session, session["board"].pins, request.query.get("q", ""), offset, limit)


@PromptServer.instance.routes.get("/papan/thumbnail/{token}/{pin_id}/{item_id}")
async def thumbnail(request):
    session = session_for(request)
    return await respond(session, session["board"].thumbnail, request.match_info["pin_id"], request.match_info["item_id"])


@PromptServer.instance.routes.post("/papan/import/{token}/{pin_id}/{item_id}")
async def import_media(request):
    session = session_for(request)
    return await respond(session, session["board"].import_media, request.match_info["pin_id"], request.match_info["item_id"], folder_paths.get_input_directory())
