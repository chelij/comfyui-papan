import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch
from uuid import uuid4
import zipfile

from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.exceptions import InvalidTag
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("papan_board", ROOT / "board.py")
board_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(board_module)


class BoardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.file = self.root / "references.papan"
        self.board_id, self.pin_id, self.image_id, self.video_id = [str(uuid4()) for _ in range(4)]
        Image.new("RGBA", (24, 12), (200, 60, 30, 90)).save(self.root / "original.png")
        Image.new("RGB", (8, 4), "blue").save(self.root / "preview.webp")
        self.data = {"format": "papan-collection", "version": 1, "revision": str(uuid4()), "collection": {"id": self.board_id, "name": "References"},
                     "pins": [{"id": self.pin_id, "collectionId": self.board_id, "title": "Warm portrait", "notes": "soft lighting", "tags": ["painting"],
                               "items": [{"id": self.image_id, "kind": "image", "localPath": str(self.root / "original.png"), "previewPath": str(self.root / "preview.webp")},
                                         {"id": self.video_id, "kind": "video", "localPath": str(ROOT / "tests/fixtures/portrait.mp4")}]}]}
        self.file.write_text(json.dumps(self.data), encoding="utf-8")

    def encrypted_file(self):
        destination = self.root / "protected.papan"
        script = '''
import { readFile } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import path from 'node:path';
import { beginVault, wrapKey } from './tests/fixtures/papan-vault.mjs';
const manifest = JSON.parse(await readFile(process.argv[1], 'utf8'));
manifest.format = 'papan-encrypted'; manifest.trash = [];
const session = await wrapKey('test password 🔑');
const writer = await beginVault(process.argv[2], session);
try {
 for (const pin of manifest.pins) for (const item of pin.items) {
  item.vault = {};
  for (const [field, kind] of [['localPath','original'], ['previewPath','preview']]) {
   if (item[field]) item.vault[kind] = await writer.add(randomUUID(), {file:item[field]}, path.extname(item[field]));
   delete item[field];
  }
 }
 await writer.finish(manifest);
} finally { await writer.close(); session.key.fill(0); }
'''
        subprocess.run(["node", "--input-type=module", "-e", script, str(self.file), str(destination)], cwd=ROOT, check=True, capture_output=True)
        return destination

    def test_search_paging_and_original_copy(self):
        board = board_module.PapanBoard(self.file)
        for query in ("PORTRAIT", "painting", "lighting"):
            self.assertEqual(board.pins(query)["total"], 1)
        self.assertEqual(board.pins("missing")["total"], 0)
        self.assertEqual(board.pins(offset=1)["pins"], [])
        result = board.import_media(self.pin_id, self.image_id, self.root / "input")
        destination = self.root / "input" / result["subfolder"] / result["name"]
        with Image.open(destination) as image:
            self.assertEqual(image.size, (24, 12))
            self.assertEqual(image.getpixel((0, 0)), (200, 60, 30, 90))
        self.assertEqual(result["quality"], "original")
        self.assertEqual(board.import_media(self.pin_id, self.image_id, self.root / "input"), result)
        self.assertEqual(len(list(destination.parent.glob("*.png"))), 1)
        self.file.unlink()
        self.assertTrue(destination.is_file())

    def test_previews_missing_media_and_video_import(self):
        board = board_module.PapanBoard(self.file)
        with Image.open(io.BytesIO(board.thumbnail(self.pin_id, self.image_id))) as image:
            self.assertEqual(image.size, (8, 4))
        (self.root / "original.png").write_text("unsupported raster original")
        self.assertEqual(board.import_media(self.pin_id, self.image_id, self.root / "input")["quality"], "preview")
        video = board.import_media(self.pin_id, self.video_id, self.root / "input")
        self.assertEqual(video["kind"], "video")
        self.assertEqual((self.root / "input" / video["subfolder"] / video["name"]).read_bytes(), (ROOT / "tests/fixtures/portrait.mp4").read_bytes())
        (self.root / "preview.webp").unlink()
        (self.root / "original.png").unlink()
        self.assertFalse(board.pins()["pins"][0]["items"][0]["available"])
        with self.assertRaises(ValueError):
            board.thumbnail(self.pin_id, self.image_id)

    def test_native_papan_encryption_wrong_password_media_and_lock(self):
        padded = self.root / "padded.mp4"
        padded.write_bytes((ROOT / "tests/fixtures/portrait.mp4").read_bytes() + bytes(2 * board_module.CHUNK + 37))
        self.data["pins"][0]["items"][1]["localPath"] = str(padded)
        self.file.write_text(json.dumps(self.data))
        board = board_module.PapanBoard(self.encrypted_file())
        self.assertTrue(board.summary()["locked"])
        with self.assertRaises(PermissionError):
            board.pins()
        with self.assertRaises(PermissionError):
            board.unlock("wrong password")
        board.unlock("test password 🔑")
        self.assertEqual(board.summary()["name"], "References")
        image = board.import_media(self.pin_id, self.image_id, self.root / "input")
        with Image.open(self.root / "input" / image["subfolder"] / image["name"]) as saved:
            self.assertEqual(saved.getpixel((0, 0)), (200, 60, 30, 90))
        self.assertEqual(board.import_media(self.pin_id, self.video_id, self.root / "input")["kind"], "video")
        self.assertEqual(board.media_bytes(board.manifest["pins"][0]["items"][1]["vault"]["original"]), padded.read_bytes())
        key = board.key
        board.close()
        self.assertEqual(bytes(key), bytes(32))
        with self.assertRaises(PermissionError):
            board.thumbnail(self.pin_id, self.image_id)

    def test_encrypted_chunk_corruption_is_rejected(self):
        file = self.encrypted_file()
        board = board_module.PapanBoard(file)
        board.unlock("test password 🔑")
        entry = board.manifest["pins"][0]["items"][0]["vault"]["original"]
        contents = bytearray(file.read_bytes())
        contents[entry["offset"] + 30] ^= 1
        file.write_bytes(contents)
        with self.assertRaises(InvalidTag):
            board.import_media(self.pin_id, self.image_id, self.root / "input")
        self.assertFalse((self.root / "input").exists())

    def test_portable_bundle_and_unsafe_paths(self):
        data = copy.deepcopy(self.data)
        data["format"] = "papan-bundle"
        data["pins"][0]["items"] = data["pins"][0]["items"][:1]
        item = data["pins"][0]["items"][0]
        item["localPath"], item["previewPath"] = "media/0-original.png", "media/1-preview.webp"
        file = self.root / "portable.papan.zip"
        with zipfile.ZipFile(file, "w") as archive:
            archive.writestr("collection.json", json.dumps(data))
            archive.write(self.root / "original.png", item["localPath"])
            archive.write(self.root / "preview.webp", item["previewPath"])
        self.assertEqual(board_module.PapanBoard(file).import_media(self.pin_id, self.image_id, self.root / "input")["quality"], "original")
        with zipfile.ZipFile(file, "a") as archive:
            archive.writestr("../escape.png", b"bad")
        with self.assertRaises(ValueError):
            board_module.PapanBoard(file)

    def test_invalid_metadata_unknown_media_and_output_symlinks(self):
        board = board_module.PapanBoard(self.file)
        with self.assertRaises(LookupError):
            board.thumbnail(self.pin_id, str(uuid4()))
        self.data["format"] = "library"
        self.file.write_text(json.dumps(self.data))
        with self.assertRaises(ValueError):
            board_module.PapanBoard(self.file)
        root = self.root / "input"
        root.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        try:
            (root / "papan").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Test symlinks unavailable")
        with self.assertRaises(ValueError):
            board.import_media(self.pin_id, self.image_id, root)
        self.assertEqual(list(outside.iterdir()), [])


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = BoardTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        routes = web.RouteTableDef()
        fake_server = types.SimpleNamespace(PromptServer=types.SimpleNamespace(instance=types.SimpleNamespace(routes=routes)))
        fake_paths = types.SimpleNamespace(get_input_directory=lambda: str(self.fixture.root / "input"), get_temp_directory=lambda: str(self.fixture.root / "temp"))
        spec = importlib.util.spec_from_file_location("papan_extension_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
        module = importlib.util.module_from_spec(spec)
        fake_nodes = types.SimpleNamespace(PapanReferences=object)
        with patch.dict(sys.modules, {"server": fake_server, "folder_paths": fake_paths, spec.name: module, f"{spec.name}.nodes": fake_nodes}):
            spec.loader.exec_module(module)
        self.module = module
        module.folder_paths = fake_paths
        self.addCleanup(module.close_sessions)
        app = web.Application()
        app.add_routes(routes)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def open_file(self, file):
        response = await self.client.post("/papan/open", json={"path": str(file)})
        self.assertEqual(response.status, 200)
        return await response.json()

    async def test_explicit_file_browse_thumbnail_import_and_close(self):
        response = await self.client.get("/papan/boards")
        self.assertEqual(response.status, 404, "There must be no automatic library scan endpoint")
        entry = await self.open_file(self.fixture.file)
        token = entry["token"]
        response = await self.client.get(f"/papan/pins/{token}?q=painting")
        self.assertEqual((await response.json())["total"], 1)
        response = await self.client.get(f"/papan/thumbnail/{token}/{self.fixture.pin_id}/{self.fixture.image_id}")
        self.assertEqual(response.content_type, "image/png")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        response = await self.client.post(f"/papan/import/{token}/{self.fixture.pin_id}/{self.fixture.video_id}")
        self.assertEqual((await response.json())["kind"], "video")
        response = await self.client.delete(f"/papan/board/{token}")
        self.assertEqual(response.status, 200)
        response = await self.client.get(f"/papan/pins/{token}")
        self.assertEqual(response.status, 401)
        self.assertTrue(self.fixture.file.is_file())

    async def test_uploaded_file_and_password_authentication(self):
        file = self.fixture.encrypted_file()
        form = FormData()
        form.add_field("file", file.read_bytes(), filename="protected.papan", content_type="application/octet-stream")
        response = await self.client.post("/papan/open", data=form)
        entry = await response.json()
        self.assertTrue(entry["locked"])
        retained = self.fixture.root / "input" / entry["source"]["input_file"]
        self.assertEqual(retained.read_bytes(), file.read_bytes())
        token = entry["token"]
        uploaded = self.module.sessions[token]["board"].file
        response = await self.client.get(f"/papan/pins/{token}")
        self.assertEqual(response.status, 401)
        response = await self.client.post(f"/papan/unlock/{token}", json={"password": "wrong"})
        self.assertEqual(response.status, 401)
        response = await self.client.post(f"/papan/unlock/{token}", json={"password": "test password 🔑"})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["name"], "References")
        response = await self.client.get(f"/papan/pins/{token}")
        self.assertEqual((await response.json())["total"], 1)
        await self.client.delete(f"/papan/board/{token}")
        self.assertFalse(uploaded.exists())
        self.assertTrue(file.exists())
        response = await self.client.post("/papan/open", json=entry["source"])
        reopened = await response.json()
        self.assertEqual(response.status, 200)
        self.assertTrue(reopened["locked"], "Retained protected boards must stay encrypted")
        self.assertNotEqual(reopened["token"], token)
        await self.client.delete(f"/papan/board/{reopened['token']}")
        self.assertTrue(retained.exists())

    async def test_bad_paths_paging_upload_and_session_expiry(self):
        response = await self.client.post("/papan/open", json={"path": "relative.papan"})
        self.assertEqual(response.status, 400)
        for filename in ("../references.papan", str(self.fixture.file)):
            response = await self.client.post("/papan/open", json={"input_file": filename})
            self.assertEqual(response.status, 400)
        entry = await self.open_file(self.fixture.file)
        token = entry["token"]
        for query in ("offset=-1", "limit=0", "limit=101", "offset=no"):
            response = await self.client.get(f"/papan/pins/{token}?{query}")
            self.assertEqual(response.status, 400)
        form = FormData()
        form.add_field("file", b"bad", filename="broken.papan", content_type="application/octet-stream")
        response = await self.client.post("/papan/open", data=form)
        self.assertEqual(response.status, 400)
        self.assertEqual(list((self.fixture.root / "temp").iterdir()), [])
        self.module.sessions[token]["touched"] = time.monotonic() - 1801
        response = await self.client.get(f"/papan/pins/{token}")
        self.assertEqual(response.status, 401)
        self.assertNotIn(token, self.module.sessions)


if __name__ == "__main__":
    unittest.main()
