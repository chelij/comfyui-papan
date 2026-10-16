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

from aiohttp import ClientSession, ClientTimeout, ClientError, web
from cryptography.exceptions import InvalidTag
import folder_paths
from server import PromptServer

from .board import PapanBoard, sessions, SESSION_SECONDS, discard, get_session, session_operation
from .nodes import PapanReferences
from .live import LibraryError, LiveBoard, connection_store, media_path


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


async def request_object(request):
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("A JSON object is required.")
    except ValueError as error:
        raise web.HTTPBadRequest(text='{"error":"Send a JSON object with valid syntax."}', content_type="application/json", headers={"Cache-Control": "no-store"}) from error
    return data


async def respond(session, operation, *args):
    try:
        result = await asyncio.to_thread(session_operation, session, operation, *args) if session else await asyncio.to_thread(operation, *args)
        if isinstance(result, bytes):
            return web.Response(body=result, content_type="image/png", headers={"Cache-Control": "no-store"})
        return web.json_response(result, headers={"Cache-Control": "no-store"})
    except LibraryError as error:
        return web.json_response({"error": str(error)}, status=error.status, headers={"Cache-Control": "no-store"})
    except PermissionError as error:
        return web.json_response({"error": str(error)}, status=401)
    except LookupError as error:
        return web.json_response({"error": str(error)}, status=404)
    except (ValueError, OSError, KeyError, TypeError, InvalidTag, zipfile.BadZipFile) as error:
        message = "Encrypted media is damaged or has changed. Open the file again." if isinstance(error, InvalidTag) else str(error)
        return web.json_response({"error": message}, status=400)


def create_session(board, owned=False):
    for token, session in list(sessions.items()):
        if time.monotonic() - session["touched"] > SESSION_SECONDS and not session["lock"].locked():
            discard(token)
    if len(sessions) >= 64:
        raise ValueError("Too many open Papan boards. Close a board before opening another.")
    token = secrets.token_urlsafe(32)
    sessions[token] = {"board": board, "owned": owned, "lock": threading.Lock(), "touched": time.monotonic()}
    return token


@PromptServer.instance.routes.post("/papan/open")
async def open_board(request):
    file, owned, filename, source = None, False, "", None
    try:
        if request.content_type == "application/json":
            data = await request_object(request)
            if "connection_id" in data:
                board = await asyncio.to_thread(LiveBoard, connection_store(), data["connection_id"], data.get("collection_id"))
                token = create_session(board)
                source = {"connection_id": board.connection_id, "collection_id": board.collection_id}
                return web.json_response({**board.summary(), "fileName": "Live from Papan", "source": source, "token": token}, headers={"Cache-Control": "no-store"})
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
        token = create_session(board, owned)
        return web.json_response({**board.summary(), "fileName": filename, "source": source, "token": token}, headers={"Cache-Control": "no-store"})
    except LibraryError as error:
        return web.json_response({"error": str(error)}, status=error.status, headers={"Cache-Control": "no-store"})
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
    if isinstance(session["board"], LiveBoard):
        return web.json_response({"error": "Unlock this collection in Papan."}, status=423)
    data = await request_object(request)
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


@PromptServer.instance.routes.get("/papan/duration/{token}/{pin_id}/{item_id}")
async def duration(request):
    session = session_for(request)
    return await respond(session, session["board"].duration, request.match_info["pin_id"], request.match_info["item_id"])


@PromptServer.instance.routes.get("/papan/connections")
async def connections(request):
    return await respond(None, lambda: {"connections": connection_store().summaries()})


@PromptServer.instance.routes.post("/papan/connections")
async def connect(request):
    data = await request_object(request)
    return await respond(None, lambda: connection_store().connect(data.get("connection"), data.get("id")))


@PromptServer.instance.routes.delete("/papan/connections/{connection_id}")
async def disconnect(request):
    connection_id = request.match_info["connection_id"]
    for token, session in list(sessions.items()):
        if isinstance(session["board"], LiveBoard) and session["board"].connection_id == connection_id:
            await asyncio.to_thread(session_operation, session, discard, token)
    def remove():
        connection_store().remove(connection_id)
        return {"disconnected": True}
    return await respond(None, remove)


@PromptServer.instance.routes.get("/papan/connections/{connection_id}/collections")
async def collections(request):
    def read():
        items, revision = connection_store().get(request.match_info["connection_id"]).collections()
        return {"collections": items, "revision": revision}
    return await respond(None, read)


async def relay(request, path, events=False):
    response = None
    try:
        store = connection_store()
        connection_id = request.match_info["connection_id"]
        connection = store.get(connection_id)
        await asyncio.to_thread(connection.check)
        headers = dict(connection.headers)
        if events and request.headers.get("Last-Event-ID"):
            headers["Last-Event-ID"] = request.headers["Last-Event-ID"][:200]
        if not events and request.headers.get("Range"):
            headers["Range"] = request.headers["Range"]
        timeout = ClientTimeout(total=None, connect=5, sock_read=30)
        async with ClientSession(timeout=timeout, trust_env=False) as client:
            async with client.request("GET" if events else request.method, connection.endpoint + path, headers=headers, allow_redirects=False) as upstream:
                if upstream.status not in (200, 206, 416):
                    messages = {401: "Papan access was revoked. Copy a new connection from Papan.", 423: "Unlock this collection in Papan.", 404: "This saved media is no longer available to ComfyUI."}
                    raise LibraryError(messages.get(upstream.status, "Papan is unavailable. Retry shortly."), upstream.status if upstream.status in (401, 404, 423, 429, 503) else 502)
                response_headers = {key: upstream.headers[key] for key in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges") if key in upstream.headers}
                response_headers["Cache-Control"] = "no-store"
                response_headers["X-Content-Type-Options"] = "nosniff"
                response = web.StreamResponse(status=upstream.status, headers=response_headers)
                await response.prepare(request)
                async for chunk in upstream.content.iter_any():
                    if request.transport is None or request.transport.is_closing() or store.get(connection_id).data != connection.data:
                        raise ConnectionResetError()
                    await response.write(chunk)
                await response.write_eof()
                return response
    except (LibraryError, ClientError, OSError, ValueError) as error:
        if response is not None:
            response.force_close()
            if request.transport:
                request.transport.close()
            return response
        status = error.status if isinstance(error, LibraryError) else 503
        message = str(error) if isinstance(error, (LibraryError, ValueError)) else "Cannot reach Papan. Keep it open and enable app connections."
        return web.json_response({"error": message}, status=status, headers={"Cache-Control": "no-store"})


@PromptServer.instance.routes.get("/papan/connections/{connection_id}/events")
async def library_events(request):
    if request.method != "GET":
        return web.Response(status=405, headers={"Allow": "GET", "Cache-Control": "no-store"})
    return await relay(request, "/v1/events", events=True)


@PromptServer.instance.routes.get("/papan/connections/{connection_id}/media/{pin_id}/{item_id}")
async def live_media(request):
    try:
        path = media_path(request.match_info["pin_id"], request.match_info["item_id"], request.query.get("variant", "original"))
    except (ValueError, TypeError, AttributeError):
        return web.json_response({"error": "Invalid Papan media request."}, status=400)
    return await relay(request, path)
