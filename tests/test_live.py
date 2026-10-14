import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from uuid import uuid4

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import test_board as fixtures

ROOT = fixtures.ROOT


package = types.ModuleType("papan_live_unit")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location("papan_live_unit.live", ROOT / "live.py")
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.connection = {"version": 1, "endpoint": "http://127.0.0.1:47779", "token": "a" * 43}

    def test_only_explicit_loopback_addresses_and_complete_keys(self):
        self.assertEqual(live.connection_input(self.connection), self.connection)
        for endpoint in ("http://localhost:47779", "http://127.0.0.2:47779", "https://127.0.0.1:47779", "http://127.0.0.1:0", "http://127.0.0.1:65536", "http://127.0.0.1:47779/", "http://user@127.0.0.1:47779", "http://127.0.0.1:47779/?redirect=evil", None):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                live.connection_input({**self.connection, "endpoint": endpoint})
        for token in (None, "", "b" * 42, "b" * 44, "b\n" * 22):
            with self.subTest(token=token), self.assertRaises(ValueError):
                live.connection_input({**self.connection, "token": token})

    def test_private_atomic_store_deduplication_restart_and_key_update(self):
        store = live.ConnectionStore(self.root)
        server_id = str(uuid4())
        collections = [{"id": str(uuid4()), "name": "Studies", "protected": False, "locked": False}]
        with patch.object(live.LibraryConnection, "check", return_value=server_id), patch.object(live.LibraryConnection, "collections", return_value=(collections, "r1")):
            first = store.connect(self.connection)
            self.assertEqual(store.connect(self.connection), first)
            self.assertEqual(len(store.summaries()), 1)
            self.assertNotIn(self.connection["token"], json.dumps(first))
            self.assertNotIn("token", json.dumps(store.summaries()))
            restored = live.ConnectionStore(self.root)
            connection_id = first["connection"]["id"]
            moved = live.ConnectionStore(self.root / "another-server")
            self.assertEqual(moved.connect(self.connection)["connection"]["id"], connection_id)
            self.assertEqual(restored.get(connection_id).data["token"], self.connection["token"])
            result = restored.connect({**self.connection, "token": "b" * 43}, connection_id)
            self.assertEqual(result["connection"]["id"], connection_id)
            with patch.object(live.LibraryConnection, "check", return_value=str(uuid4())):
                with self.assertRaises(live.LibraryError):
                    restored.connect(self.connection, connection_id)
            self.assertEqual(restored.get(connection_id).data["token"], "b" * 43)
            restored.remove(connection_id)
            with self.assertRaises(live.LibraryError):
                restored.get(connection_id)
            self.assertEqual(restored.connect(self.connection)["connection"]["id"], connection_id, "Reconnecting a forgotten library must preserve saved workflow identities")
        if os.name != "nt":
            self.assertEqual(store.file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(store.directory.glob("*.tmp")), [])

    def test_invalid_saved_settings_are_preserved(self):
        store = live.ConnectionStore(self.root)
        store.directory.mkdir()
        original = b'{"version":1,"connections":[{"token":"secret"}]}'
        store.file.write_bytes(original)
        with self.assertRaisesRegex(ValueError, "preserved"):
            live.ConnectionStore(self.root)
        self.assertEqual(store.file.read_bytes(), original)

    def test_pagination_restarts_on_revision_change_and_rejects_bad_cursors(self):
        connection = live.LibraryConnection(self.connection)
        def page(revision, items, total, offset):
            return {"version": 1, "revision": revision, "items": items, "total": total, "nextOffset": offset}
        with patch.object(connection, "json", side_effect=[page("r1", [1], 2, 1), page("r2", [2], 2, None), page("r2", [3], 2, 1), page("r2", [4], 2, None)]) as read:
            self.assertEqual(connection.pages("/v1/pins"), ([3, 4], "r2"))
            self.assertIn("offset=0", read.call_args_list[2].args[0])
        for invalid in (-1, 0, 2, "1"):
            with patch.object(connection, "json", return_value=page("r", [1], 2, invalid)), self.assertRaises(live.LibraryError):
                connection.pages("/v1/pins")


@unittest.skipUnless(os.environ.get("PAPAN_DESKTOP_DIR"), "Set PAPAN_DESKTOP_DIR to test the actual desktop library service.")
class LiveServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.BoardTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        pose_id = str(uuid4())
        self.fixture.data["pins"][0]["items"].append({"id": pose_id, "kind": "video", "poseFor": self.fixture.video_id, "localPath": str(ROOT / "tests/fixtures/portrait.mp4")})
        self.fixture.file.write_text(json.dumps(self.fixture.data))
        self.process = await asyncio.create_subprocess_exec("node", str(ROOT / "tests/fixtures/live-library.mjs"), os.environ["PAPAN_DESKTOP_DIR"], str(self.fixture.file), str(self.fixture.root / "library"), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self.addAsyncCleanup(self.stop_service)
        self.description = json.loads(await asyncio.wait_for(self.process.stdout.readline(), 10))
        routes = web.RouteTableDef()
        fake_server = types.SimpleNamespace(PromptServer=types.SimpleNamespace(instance=types.SimpleNamespace(routes=routes)))
        self.fake_paths = types.SimpleNamespace(get_input_directory=lambda: str(self.fixture.root / "input"), get_temp_directory=lambda: str(self.fixture.root / "temp"), get_user_directory=lambda: str(self.fixture.root / "user"))
        spec = importlib.util.spec_from_file_location("papan_live_route_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"server": fake_server, "folder_paths": self.fake_paths, spec.name: module, f"{spec.name}.nodes": types.SimpleNamespace(PapanReferences=object)}):
            spec.loader.exec_module(module)
            self.live = sys.modules[f"{spec.name}.live"]
            self.live._store = self.live.ConnectionStore(self.fixture.root / "user")
        self.module = module
        module.folder_paths = self.fake_paths
        self.addCleanup(module.close_sessions)
        app = web.Application()
        api_routes = web.RouteTableDef()
        for route in routes:
            api_routes.route(route.method, "/api" + route.path)(route.handler, **route.kwargs)
        app.add_routes(api_routes)
        app.add_routes(routes)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        response = await self.client.post("/papan/connections", json={"connection": self.description["connection"]})
        self.assertEqual(response.status, 200, await response.text())
        self.saved = await response.json()
        self.connection_id = self.saved["connection"]["id"]

    async def stop_service(self):
        if self.process.returncode is None:
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()

    async def command(self, action, **data):
        self.process.stdin.write((json.dumps({"action": action, **data}) + "\n").encode())
        await self.process.stdin.drain()
        result = json.loads(await asyncio.wait_for(self.process.stdout.readline(), 10))
        self.assertNotIn("error", result)
        return result

    async def open_collection(self, index=0):
        source = {"connection_id": self.connection_id, "collection_id": self.description["collections"][index]["id"]}
        response = await self.client.post("/papan/open", json=source)
        self.assertEqual(response.status, 200, await response.text())
        return await response.json()

    async def test_scopes_thumbnails_pose_original_duration_and_range_playback(self):
        self.assertEqual(len(self.saved["collections"]), 2)
        self.assertTrue(self.saved["collections"][1]["locked"])
        self.assertNotIn(self.description["connection"]["token"], json.dumps(self.saved))
        entry = await self.open_collection()
        self.assertTrue(entry["live"])
        self.assertNotIn(self.description["connection"]["token"], json.dumps(entry))
        token = entry["token"]
        response = await self.client.get(f"/papan/pins/{token}")
        pin = (await response.json())["pins"][0]
        self.assertEqual(pin["items"][2]["poseFor"], pin["items"][1]["id"])
        self.assertNotIn(str(self.fixture.root), json.dumps(pin))
        response = await self.client.get(f"/papan/thumbnail/{token}/{pin['id']}/{pin['items'][0]['id']}")
        self.assertEqual(response.content_type, "image/png")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        for item in pin["items"][1:]:
            response = await self.client.get(f"/papan/duration/{token}/{pin['id']}/{item['id']}")
            self.assertEqual(await response.json(), {"seconds": 1.0})
        self.assertFalse((self.fixture.root / "input").exists())
        route = f"/papan/connections/{self.connection_id}/media/{pin['id']}/{pin['items'][1]['id']}"
        response = await self.client.get(route, headers={"Range": "bytes=13-29", "Origin": "http://comfy.example"})
        self.assertEqual(response.status, 206)
        self.assertEqual(await response.read(), (ROOT / "tests/fixtures/portrait.mp4").read_bytes()[13:30])
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        response = await self.client.head(route)
        self.assertEqual(response.status, 200)
        self.assertEqual(int(response.headers["Content-Length"]), (ROOT / "tests/fixtures/portrait.mp4").stat().st_size)
        self.assertEqual(await response.read(), b"")
        response = await self.client.post(f"/papan/import/{token}/{pin['id']}/{pin['items'][0]['id']}")
        imported = await response.json()
        self.assertEqual(imported["quality"], "original")
        hidden = {"connection_id": self.connection_id, "collection_id": self.description["collections"][2]["id"]}
        self.assertEqual((await self.client.post("/papan/open", json=hidden)).status, 404)

    async def test_locked_and_revoked_access_never_uses_imported_copy(self):
        entry = await self.open_collection(1)
        self.assertTrue(entry["locked"])
        token = entry["token"]
        self.assertEqual((await self.client.post(f"/papan/unlock/{token}", json={"password": "ignored"})).status, 423)
        await self.command("unlock")
        entry = await self.open_collection(1)
        token = entry["token"]
        response = await self.client.get(f"/papan/pins/{token}")
        pin = (await response.json())["pins"][0]
        item = pin["items"][1]
        route = f"/papan/import/{token}/{pin['id']}/{item['id']}"
        response = await self.client.post(route)
        self.assertEqual(response.status, 200)
        imported = await response.json()
        copy = self.fixture.root / "input" / imported["subfolder"] / imported["name"]
        self.assertTrue(copy.is_file())
        await self.command("lock")
        self.assertEqual((await self.client.post(route)).status, 423)
        await self.command("unlock")
        await self.command("scope")
        self.assertEqual((await self.client.post(route)).status, 404)
        await self.command("revoke")
        response = await self.client.get(f"/papan/connections/{self.connection_id}/collections")
        self.assertEqual(response.status, 401)
        self.assertTrue(copy.is_file(), "Files already delivered remain explicit ComfyUI input copies")

    async def test_sse_changes_cursor_restart_and_saved_credentials(self):
        route = f"/papan/connections/{self.connection_id}/events"
        self.assertEqual((await self.client.head(route)).status, 405)
        response = await self.client.get(route)
        async def next_event():
            block = await asyncio.wait_for(response.content.readuntil(b"\n\n"), 5)
            line = next(line for line in block.decode().splitlines() if line.startswith("data: "))
            return json.loads(line[6:])
        first = await next_event()
        self.assertTrue(first["reset"])
        await self.command("edit")
        changed = await next_event()
        self.assertNotEqual(first["revision"], changed["revision"])
        response.close()
        response = await self.client.get(route, headers={"Last-Event-ID": changed["revision"]})
        self.assertFalse((await next_event())["reset"])
        response.close()
        await self.command("restart")
        response = await self.client.get(route, headers={"Last-Event-ID": changed["revision"]})
        self.assertTrue((await next_event())["reset"])
        response.close()
        store = self.live.ConnectionStore(self.fixture.root / "user")
        self.assertEqual(store.get(self.connection_id).collections()[0][1]["locked"], True)

    async def test_node_execution_rechecks_live_access_even_with_saved_filename(self):
        spec = importlib.util.spec_from_file_location("papan_live_route_test.nodes_real", ROOT / "nodes.py")
        module = importlib.util.module_from_spec(spec)
        class Video:
            def __init__(self, file): self.file = Path(file)
            def get_duration(self): return 1.0
        fake_api = types.SimpleNamespace(InputImpl=types.SimpleNamespace(VideoFromFile=Video))
        with patch.dict(sys.modules, {"papan_live_route_test": self.module, "papan_live_route_test.live": self.live, "papan_live_route_test.board": sys.modules.get("papan_live_unit.board"), "folder_paths": self.fake_paths, "nodes": types.SimpleNamespace(), "comfy_api.latest": fake_api}):
            spec.loader.exec_module(module)
        await self.command("unlock")
        entry = await self.open_collection(1)
        response = await self.client.get(f"/papan/pins/{entry['token']}")
        pin = (await response.json())["pins"][0]
        reference = {"boardId": entry["id"], "connectionId": self.connection_id, "collectionId": entry["source"]["collection_id"], "pinId": pin["id"], "itemId": pin["items"][1]["id"], "title": "Clip", "kind": "video"}
        state = json.dumps([reference, {"kind": "seconds", "title": "Clip seconds", "source_slot": 0}])
        result = await module.PapanReferences().load(state)
        self.assertEqual(result["result"][1], 1.0)
        self.assertEqual(result["ui"]["papan_imported"], [])
        reference["filename"] = result["result"][0].file.relative_to(self.fixture.root / "input").as_posix()
        state = json.dumps([reference])
        self.assertTrue(__import__("math").isnan(module.PapanReferences.IS_CHANGED(state)))
        self.assertTrue(__import__("math").isnan(module.PapanReferences.IS_CHANGED(state, prompt={}, unique_id="1")))
        await self.command("lock")
        with self.assertRaises(self.live.LibraryError):
            await module.PapanReferences().load(state)


if __name__ == "__main__":
    unittest.main()
