import http.client
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
from urllib.parse import urlencode, urlsplit
from uuid import UUID

import av
from PIL import Image

from .board import MAX_MEDIA, MAX_METADATA, PapanBoard, decode_media, validate_manifest


ENDPOINT = re.compile(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})\Z")
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
VIDEO_TYPES = {"video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov", "video/x-matroska": ".mkv", "video/x-msvideo": ".avi", "video/mp2t": ".ts"}
_store = None
_store_lock = threading.Lock()


def connection_store():
    global _store
    with _store_lock:
        if _store is None:
            import folder_paths
            _store = ConnectionStore(folder_paths.get_user_directory())
    return _store


class LibraryError(Exception):
    def __init__(self, message, status=503):
        super().__init__(message)
        self.status = status


def connection_input(data):
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("Paste the connection copied from Papan's Connect apps window.")
    endpoint = data.get("endpoint", "")
    match = ENDPOINT.fullmatch(endpoint) if isinstance(endpoint, str) else None
    if not match or int(match[1]) > 65535:
        raise ValueError("Papan must run on the same computer as the ComfyUI server. Use http://127.0.0.1:port.")
    token = data.get("token", "")
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise ValueError("The Papan connection is missing its app access key.")
    return {"version": 1, "endpoint": endpoint, "token": token}


class LibraryConnection:
    def __init__(self, data):
        self.data = dict(data)
        self.endpoint = data["endpoint"]
        self.headers = {"Authorization": "Bearer " + data["token"]}

    def read(self, path, limit=MAX_METADATA):
        # HTTPConnection ignores proxy environment variables and never follows redirects.
        connection = http.client.HTTPConnection("127.0.0.1", urlsplit(self.endpoint).port, timeout=20)
        try:
            connection.request("GET", path, headers=self.headers)
            response = connection.getresponse()
            if response.status != 200:
                status = response.status
                message = {401: "Papan access was revoked. Copy a new connection from Papan.",
                           404: "This collection or saved media is no longer available to ComfyUI.",
                           423: "Unlock this collection in Papan.",
                           503: "Papan is changing collection protection. Retry shortly."}.get(status, "Papan could not complete this request.")
                raise LibraryError(message, status if status in (401, 404, 423, 429, 503) else 502)
            length = response.getheader("Content-Length")
            if length is not None and (not length.isdecimal() or int(length) > limit):
                raise LibraryError("Papan returned a file larger than the supported limit.", 502)
            chunks, size = [], 0
            while chunk := response.read(min(1024 * 1024, limit + 1 - size)):
                size += len(chunk)
                if size > limit:
                    raise LibraryError("Papan returned a file larger than the supported limit.", 502)
                chunks.append(chunk)
            if length is not None and size != int(length):
                raise LibraryError("The Papan transfer was interrupted. Retry while the collection is unlocked.", 502)
            return b"".join(chunks), response.getheader("Content-Type", "").split(";", 1)[0]
        except (OSError, http.client.HTTPException) as error:
            raise LibraryError("Cannot reach Papan. Keep Papan open and enable app connections on the ComfyUI server's computer.") from error
        finally:
            connection.close()

    def json(self, path):
        contents, _ = self.read(path)
        try:
            data = json.loads(contents)
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except (ValueError, UnicodeError) as error:
            raise LibraryError("Papan returned invalid library data.", 502) from error

    def check(self):
        data = self.json("/v1")
        try:
            UUID(data["serverId"])
            if data.get("service") != "papan-library" or data.get("version") != 1 or not {"collections", "pins", "media", "events"}.issubset(data.get("capabilities", [])):
                raise ValueError()
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise LibraryError("This is not a compatible Papan library service.", 502) from error
        if self.data.get("serverId") and self.data["serverId"] != data["serverId"]:
            raise LibraryError("A different Papan library is using this address. Create a separate connection for it.", 409)
        return data["serverId"]

    def pages(self, path, parameters=None):
        for _ in range(3):
            items, offset, revision = [], 0, None
            while True:
                page = self.json(path + "?" + urlencode({**(parameters or {}), "offset": offset, "limit": 200}))
                current = page.get("revision")
                if page.get("version") != 1 or not isinstance(current, str) or not isinstance(page.get("items"), list) or type(page.get("total")) is not int:
                    raise LibraryError("Papan returned invalid library data.", 502)
                if revision is not None and revision != current:
                    break
                revision = current
                items.extend(page["items"])
                if len(items) > 50000:
                    raise LibraryError("This connection exceeds the 50,000 pin limit.", 400)
                next_offset = page.get("nextOffset")
                if next_offset is None:
                    if len(items) != page["total"]:
                        break
                    return items, revision
                if type(next_offset) is not int or next_offset != offset + len(page["items"]) or next_offset <= offset or next_offset > page["total"]:
                    raise LibraryError("Papan returned invalid pagination.", 502)
                offset = next_offset
        raise LibraryError("Papan changed while loading the board. Retry once its changes finish.", 409)

    def collections(self):
        self.check()
        items, revision = self.pages("/v1/collections")
        try:
            for item in items:
                UUID(item["id"])
                if not isinstance(item["name"], str) or type(item["locked"]) is not bool or type(item["protected"]) is not bool:
                    raise ValueError()
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise LibraryError("Papan returned invalid collection data.", 502) from error
        return items, revision


class ConnectionStore:
    def __init__(self, user_directory):
        self.directory = Path(user_directory) / "papan"
        self.file = self.directory / "connections.json"
        self.lock = threading.RLock()
        self.records = []
        if self.file.exists():
            try:
                data = json.loads(self.file.read_text(encoding="utf-8"))
                if data.get("version") != 1 or not isinstance(data.get("connections"), list) or len(data["connections"]) > 32:
                    raise ValueError()
                for item in data["connections"]:
                    connection_input(item)
                    UUID(item["id"])
                    UUID(item["serverId"])
                if len({item["id"] for item in data["connections"]}) != len(data["connections"]):
                    raise ValueError()
                self.records = data["connections"]
            except (ValueError, KeyError, TypeError, AttributeError) as error:
                raise ValueError("Could not read Papan connections. The existing settings file was preserved.") from error

    def summaries(self):
        with self.lock:
            return [{key: item[key] for key in ("id", "endpoint", "serverId")} for item in self.records]

    def get(self, connection_id):
        with self.lock:
            record = next((item for item in self.records if item["id"] == connection_id), None)
            if record is None:
                raise LibraryError("Connect Papan on this ComfyUI server to reopen this workflow.", 404)
            return LibraryConnection(record)

    def save(self, records):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.directory, suffix=".tmp", mode="w", encoding="utf-8", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({"version": 1, "connections": records}, stream)
            os.replace(temporary, self.file)
            self.records = records
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    def connect(self, data, connection_id=None):
        record = connection_input(data)
        connection = LibraryConnection(record)
        record["serverId"] = connection.check()
        collections, revision = connection.collections()
        with self.lock:
            old = self.get(connection_id).data if connection_id else next((item for item in self.records if item["serverId"] == record["serverId"]), None)
            if old and old["serverId"] != record["serverId"]:
                raise LibraryError("This workflow belongs to a different Papan library. Add a new connection instead.", 409)
            record["id"] = old["id"] if old else record["serverId"]
            if not old and len(self.records) >= 32:
                raise ValueError("Disconnect an unused Papan connection before adding another.")
            self.save([item for item in self.records if item["id"] != record["id"]] + [record])
        return {"connection": {key: record[key] for key in ("id", "endpoint", "serverId")}, "collections": collections, "revision": revision}

    def remove(self, connection_id):
        with self.lock:
            self.save([item for item in self.records if item["id"] != connection_id])


def media_path(pin_id, item_id, variant):
    UUID(pin_id)
    UUID(item_id)
    if variant not in ("original", "preview"):
        raise ValueError("Choose original or preview media.")
    return f"/v1/pins/{pin_id}/items/{item_id}/media?variant={variant}"


class LiveBoard(PapanBoard):
    def __init__(self, store, connection_id, collection_id):
        UUID(connection_id)
        UUID(collection_id)
        self.store, self.connection_id, self.collection_id = store, connection_id, collection_id
        self.connection = store.get(connection_id)
        self.key, self.manifest = None, None
        self.encrypted, self.bundle = False, False
        self.collection, self.revision = self.current_collection()
        if not self.collection["locked"]:
            pins, self.revision = self.connection.pages("/v1/pins", {"collectionId": collection_id})
            # Validate identities and pose relationships with the saved-format reader.
            self.manifest = validate_manifest({"format": "papan-collection", "version": 1, "collection": self.collection, "pins": pins})

    def current_collection(self):
        self.connection = self.store.get(self.connection_id)
        collections, revision = self.connection.collections()
        collection = next((item for item in collections if item["id"] == self.collection_id), None)
        if collection is None:
            raise LibraryError("This collection is no longer shared with ComfyUI. Change its access in Papan.", 404)
        return collection, revision

    def summary(self):
        return {"id": self.connection_id + ":" + self.collection_id, "name": self.collection["name"], "revision": self.revision,
                "encrypted": self.collection["protected"], "locked": self.collection["locked"], "live": True}

    def require_open(self):
        collection, _ = self.current_collection()
        if collection["locked"] or self.manifest is None:
            raise LibraryError("Unlock this collection in Papan, then refresh its board.", 423)

    def sources(self, item, original=False):
        return [(variant, variant) for variant in (("original", "preview") if original else ("preview", "original")) if item.get(variant + "Url")]

    def media(self, pin_id, item_id, original=False):
        self.require_open()
        pin = next((pin for pin in self.manifest["pins"] if pin["id"] == pin_id), None)
        item = next((item for item in pin["items"] if item["id"] == item_id and item["kind"] in ("image", "video")), None) if pin else None
        if item is None:
            raise LibraryError("This saved reference was removed from Papan. Choose another reference.", 404)
        for variant, quality in self.sources(item, original):
            try:
                contents, content_type = self.connection.read(media_path(pin_id, item_id, variant), MAX_MEDIA)
                frame, extension = decode_media(contents, item["kind"], VIDEO_TYPES.get(content_type))
                return frame, contents, extension, item["kind"], quality
            except LibraryError as error:
                if error.status != 404:
                    raise
            except (ValueError, OSError, av.FFmpegError, StopIteration, Image.DecompressionBombError):
                continue
        raise ValueError("No readable saved media. Restore it in Papan.")

    def duration(self, pin_id, item_id):
        self.require_open()
        pin = next((pin for pin in self.manifest["pins"] if pin["id"] == pin_id), None)
        item = next((item for item in pin["items"] if item["id"] == item_id and item["kind"] == "video"), None) if pin else None
        seconds = item.get("duration") if item else None
        if isinstance(seconds, (int, float)) and math.isfinite(seconds) and seconds > 0:
            return {"seconds": float(seconds)}
        return super().duration(pin_id, item_id)
