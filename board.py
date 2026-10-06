import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import time
from uuid import UUID
import zipfile

import av
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from PIL import Image, ImageOps, UnidentifiedImageError


MAGIC = b"PAPANENC"
HEADER = 100
CHUNK = 1024 * 1024
MAX_METADATA = 64 * CHUNK
MAX_MEDIA = 512 * CHUNK
MEDIA_NAME = re.compile(r"media/[0-9]+-(preview|original)\.(jpg|jpeg|png|webp|gif|avif|svg|heic|mp4|webm|mov|m4v|mkv|avi|ts)\Z")
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".m4v", ".mkv", ".avi", ".ts"}
sessions = {}
SESSION_SECONDS = 30 * 60


def discard(token):
    session = sessions.pop(token, None)
    if session:
        session["board"].close()
        if session["owned"]:
            session["board"].file.unlink(missing_ok=True)


def get_session(token):
    session = sessions.get(token)
    if not session or time.monotonic() - session["touched"] > SESSION_SECONDS:
        if session and not session["lock"].locked():
            discard(token)
        raise PermissionError("Papan file session expired. Reopen or unlock the board on its node.")
    session["touched"] = time.monotonic()
    return session


def session_operation(session, operation, *args):
    with session["lock"]:
        return operation(*args)


def unseal(record, key, context):
    if len(record) < 28:
        raise ValueError("Incomplete encrypted record.")
    return AESGCM(key).decrypt(record[:12], record[28:] + record[12:28], context)


def validate_manifest(data, encrypted=False, bundle=False):
    expected = "papan-encrypted" if encrypted else "papan-bundle" if bundle else "papan-collection"
    if not isinstance(data, dict) or data.get("format") != expected or data.get("version") != 1:
        raise ValueError("This is not a supported Papan board file.")
    board = data.get("collection")
    if not isinstance(board, dict) or not isinstance(board.get("name"), str) or not isinstance(data.get("pins"), list):
        raise ValueError("Invalid Papan board metadata.")
    UUID(board["id"])
    pins = set()
    for pin in data["pins"]:
        UUID(pin["id"])
        if pin["id"] in pins or pin.get("collectionId") != board["id"] or not isinstance(pin.get("title"), str) or not isinstance(pin.get("items"), list) or len(pin["items"]) > 50:
            raise ValueError("Invalid Papan pin metadata.")
        if not isinstance(pin.get("tags", []), list) or any(not isinstance(tag, str) for tag in pin.get("tags", [])) or not isinstance(pin.get("notes", ""), str):
            raise ValueError("Invalid Papan pin details.")
        pins.add(pin["id"])
        items = set()
        for item in pin["items"]:
            UUID(item["id"])
            if item["id"] in items or item.get("kind") not in ("image", "video", "text"):
                raise ValueError("Invalid Papan media metadata.")
            items.add(item["id"])
            if encrypted and any(item.get(field) for field in ("previewPath", "localPath", "previewFile", "localFile")):
                raise ValueError("Encrypted boards cannot reference unencrypted files.")
            if not encrypted and item.get("vault"):
                raise ValueError("Encrypted media requires its protected Papan file.")
            for field in ("previewPath", "localPath"):
                value = item.get(field)
                if value is not None and (not isinstance(value, str) or "\0" in value or (bundle and not MEDIA_NAME.fullmatch(value)) or (not bundle and not Path(value).is_absolute())):
                    raise ValueError("Invalid saved media path.")
    return data


class PapanBoard:
    def __init__(self, file):
        self.file = Path(file)
        self.key = None
        self.manifest = None
        self.encrypted = False
        self.bundle = False
        if not self.file.is_file() or not self.file.name.lower().endswith((".papan", ".papan.zip")):
            raise ValueError("Choose a .papan or .papan.zip board file.")
        with self.file.open("rb") as stream:
            magic = stream.read(8)
        if magic == MAGIC:
            self.encrypted = True
            self.vault_header()
        elif zipfile.is_zipfile(self.file):
            self.bundle = True
            with zipfile.ZipFile(self.file) as archive:
                entries = archive.infolist()
                names = [entry.filename for entry in entries]
                if len(names) > 50001 or len(names) != len(set(names)) or "collection.json" not in names:
                    raise ValueError("Invalid portable Papan board.")
                for entry in entries:
                    limit = MAX_METADATA if entry.filename == "collection.json" else MAX_MEDIA
                    if entry.is_dir() or entry.flag_bits & 1 or not 0 < entry.file_size <= limit or (entry.filename != "collection.json" and not MEDIA_NAME.fullmatch(entry.filename)):
                        raise ValueError("Invalid portable media entry.")
                if sum(entry.file_size for entry in entries) > 20 * 1024 * CHUNK:
                    raise ValueError("Portable board exceeds the 20 GiB limit.")
                self.manifest = validate_manifest(json.loads(archive.read("collection.json")), bundle=True)
                for pin in self.manifest["pins"]:
                    for item in pin["items"]:
                        for field in ("previewPath", "localPath"):
                            if item.get(field) and item[field] not in names:
                                raise ValueError("The portable board is missing saved media.")
        else:
            if self.file.stat().st_size > MAX_METADATA:
                raise ValueError("Papan board metadata exceeds 64 MiB.")
            self.manifest = validate_manifest(json.loads(self.file.read_text(encoding="utf-8")))

    def summary(self):
        return {"id": self.manifest["collection"]["id"] if self.manifest else None,
                "name": self.manifest["collection"]["name"] if self.manifest else self.file.name,
                "revision": self.manifest.get("revision") if self.manifest else None,
                "encrypted": self.encrypted, "locked": self.manifest is None}

    def vault_header(self):
        with self.file.open("rb") as stream:
            header = stream.read(HEADER)
        if len(header) != HEADER or header[:8] != MAGIC:
            raise ValueError("Incomplete encrypted Papan board.")
        offset, length = int.from_bytes(header[84:92], "big"), int.from_bytes(header[92:100], "big")
        if offset < HEADER or not 28 <= length <= MAX_METADATA or offset + length != self.file.stat().st_size:
            raise ValueError("Invalid encrypted Papan board size.")
        return header, offset, length

    def unlock(self, password):
        if not self.encrypted:
            return self.summary()
        if not isinstance(password, str) or len(password.encode("utf-16-le")) // 2 > 1024:
            raise ValueError("Invalid password.")
        header, offset, length = self.vault_header()
        salt = header[8:24]
        derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=131072, r=8, p=1, dklen=32, maxmem=256 * CHUNK)
        key = None
        try:
            key = bytearray(unseal(header[24:84], derived, MAGIC + salt))
            if len(key) != 32:
                raise ValueError("Invalid encrypted board key.")
            with self.file.open("rb") as stream:
                stream.seek(offset)
                data = unseal(stream.read(length), key, header)
            manifest = validate_manifest(json.loads(data), encrypted=True)
        except InvalidTag as error:
            if key:
                key[:] = bytes(len(key))
            raise PermissionError("Incorrect password or damaged Papan file.") from error
        except (ValueError, KeyError, TypeError):
            if key:
                key[:] = bytes(len(key))
            raise
        self.close()
        self.key, self.manifest = key, manifest
        return self.summary()

    def close(self):
        if self.key:
            self.key[:] = bytes(len(self.key))
        self.key = None
        self.manifest = None

    def require_open(self):
        if self.manifest is None:
            raise PermissionError("Unlock this Papan file first.")

    def sources(self, item, original=False):
        sources = []
        for kind in ("original", "preview") if original else ("preview", "original"):
            if self.encrypted:
                entry = item.get("vault", {}).get(kind)
                if entry:
                    sources.append((entry, kind))
            else:
                saved = item.get("localPath" if kind == "original" else "previewPath")
                if saved and (self.bundle or Path(saved).is_file()):
                    sources.append((saved, kind))
        return sources

    def pins(self, query="", offset=0, limit=40):
        self.require_open()
        matches = []
        for pin in self.manifest["pins"]:
            searchable = " ".join(str(pin.get(field, "")) for field in ("title", "notes", "tags", "author", "text")).casefold()
            if query.casefold() not in searchable:
                continue
            items = []
            for item in pin["items"]:
                if item["kind"] not in ("image", "video"):
                    continue
                sources = self.sources(item, original=True)
                items.append({"id": item["id"], "kind": item["kind"], "available": bool(sources), "quality": sources[0][1] if sources else "missing"})
            if items:
                matches.append({"id": pin["id"], "title": pin["title"], "tags": pin.get("tags", []), "notes": pin.get("notes", ""), "items": items})
        return {"pins": matches[offset:offset + limit], "total": len(matches)}

    def media_bytes(self, source):
        if self.encrypted:
            if not isinstance(source, dict):
                raise ValueError("Invalid encrypted media entry.")
            UUID(source["id"])
            offset, size = source.get("offset"), source.get("size")
            if type(offset) is not int or type(size) is not int or offset < HEADER or not 1 <= size <= MAX_MEDIA:
                raise ValueError("Invalid encrypted media range.")
            _, manifest_offset, _ = self.vault_header()
            if offset + size + ((size + CHUNK - 1) // CHUNK) * 28 > manifest_offset:
                raise ValueError("Encrypted media is outside the board.")
            output = io.BytesIO()
            with self.file.open("rb") as stream:
                stream.seek(offset)
                for index, start in enumerate(range(0, size, CHUNK)):
                    record = stream.read(min(CHUNK, size - start) + 28)
                    output.write(unseal(record, self.key, f"{source['id']}:{index}:{size}".encode()))
            return output.getvalue()
        if self.bundle:
            with zipfile.ZipFile(self.file) as archive:
                entry = archive.getinfo(source)
                if entry.file_size > MAX_MEDIA or not MEDIA_NAME.fullmatch(source):
                    raise ValueError("Invalid portable media entry.")
                return archive.read(entry)
        file = Path(source)
        if file.stat().st_size > MAX_MEDIA:
            raise ValueError("Media exceeds the 512 MiB limit.")
        return file.read_bytes()

    def media(self, pin_id, item_id, original=False):
        self.require_open()
        pin = next((pin for pin in self.manifest["pins"] if pin["id"] == pin_id), None)
        item = next((item for item in pin["items"] if item["id"] == item_id and item["kind"] in ("image", "video")), None) if pin else None
        if item is None:
            raise LookupError("Saved media not found.")
        for source, quality in self.sources(item, original):
            try:
                contents = self.media_bytes(source)
                if item["kind"] == "image":
                    with Image.open(io.BytesIO(contents)) as image:
                        frame = ImageOps.exif_transpose(image).convert("RGBA")
                    extension = ".png"
                else:
                    extension = source["ext"] if self.encrypted else Path(source).suffix.lower()
                    if extension not in VIDEO_EXTENSIONS:
                        raise ValueError("Unsupported saved video format.")
                    with av.open(io.BytesIO(contents)) as container:
                        frame = next(container.decode(video=0)).to_image().convert("RGBA")
                return frame, contents, extension, item["kind"], quality
            except (UnidentifiedImageError, OSError, Image.DecompressionBombError, av.FFmpegError, StopIteration):
                continue
        raise ValueError("No readable saved media. Restore it in Papan or export a portable copy.")

    def thumbnail(self, pin_id, item_id):
        frame, _, _, _, _ = self.media(pin_id, item_id)
        frame.thumbnail((384, 384), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        frame.save(output, format="PNG")
        return output.getvalue()

    def import_media(self, pin_id, item_id, input_dir):
        frame, contents, extension, kind, quality = self.media(pin_id, item_id, original=True)
        if kind == "image":
            output = io.BytesIO()
            frame.save(output, format="PNG")
            contents = output.getvalue()
        name = f"papan-{hashlib.sha256(contents).hexdigest()}{extension}"
        root = Path(input_dir).resolve()
        directory = root / "papan"
        directory.mkdir(parents=True, exist_ok=True)
        if not directory.resolve().is_relative_to(root):
            raise ValueError("ComfyUI input/papan must stay inside the input directory.")
        destination = directory / name
        if destination.is_symlink():
            raise ValueError("The reference destination must not be a symbolic link.")
        if not destination.is_file() or destination.read_bytes() != contents:
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=directory, suffix=".tmp", delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(contents)
                os.replace(temporary, destination)
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
        return {"name": name, "subfolder": "papan", "type": "input", "kind": kind, "quality": quality}
