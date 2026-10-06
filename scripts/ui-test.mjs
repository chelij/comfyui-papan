import assert from 'node:assert/strict';
import { execFile, spawn } from 'node:child_process';
import { cp, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { promisify } from 'node:util';
import { chromium } from 'playwright';

const comfy = process.env.COMFYUI_DIR;
if (!comfy) throw new Error('Set COMFYUI_DIR to a ComfyUI checkout with its .venv installed.');
const python = path.join(comfy, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const run = promisify(execFile), directory = await mkdtemp(path.join(tmpdir(), 'papan-ui-'));
const base = path.join(directory, 'comfy'), port = Number(process.env.PAPAN_TEST_PORT || 18389), url = `http://127.0.0.1:${port}`;
const vue = process.env.PAPAN_VUE_NODES === '1';
let server, browser, page, log = '';
try {
  try { await fetch(`${url}/system_stats`); throw new Error(`Test port ${port} is occupied.`); }
  catch (error) { if (!error.message.includes('fetch failed')) throw error; }
  await mkdir(path.join(base, 'custom_nodes'), { recursive: true });
  await mkdir(path.join(base, 'user', 'default'), { recursive: true });
  await mkdir('artifacts', { recursive: true });
  await writeFile(path.join(base, 'user', 'default', 'comfy.settings.json'), JSON.stringify({ 'Comfy.VueNodes.Enabled': vue }));
  if (process.env.PAPAN_EXTENSION_DIR) {
    await cp(process.env.PAPAN_EXTENSION_DIR, path.join(base, 'custom_nodes', 'comfyui-papan'), { recursive: true, filter: source => !['.git', 'node_modules', '__pycache__'].includes(path.basename(source)) });
  } else {
    await run(python, ['scripts/package.py']);
    const version = (await readFile('VERSION', 'utf8')).trim();
    await run(python, ['-c', 'import sys,zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])', `dist/comfyui-papan-v${version}.zip`, path.join(base, 'custom_nodes')]);
  }
  const init = path.join(base, 'custom_nodes', 'comfyui-papan', '__init__.py');
  await writeFile(init, await readFile(init, 'utf8') + `
class PapanDurationTest:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"seconds": ("FLOAT", {"forceInput": True})}}
    RETURN_TYPES = ()
    FUNCTION = "load"
    OUTPUT_NODE = True
    def load(self, seconds):
        return {"result": (), "ui": {"seconds": [seconds]}}
NODE_CLASS_MAPPINGS["PapanDurationTest"] = PapanDurationTest
class PapanMediaTest:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"media": ("IMAGE", {"forceInput": True})}}
    RETURN_TYPES = ()
    FUNCTION = "load"
    def load(self, media):
        return ()
NODE_CLASS_MAPPINGS["PapanMediaTest"] = PapanMediaTest
`);
  await run(python, ['-c', `import json,sys,zipfile
from pathlib import Path
from uuid import uuid4
p=Path(sys.argv[1])
with zipfile.ZipFile('examples/demo.papan.zip') as z:
 z.extractall(p)
d=json.loads((p/'collection.json').read_text());d['format']='papan-collection'
for pin in d['pins']:
 for item in pin['items']:item['localPath']=str(p/item['localPath'])
for index in range(40):
 d['pins'].append({'id':str(uuid4()),'collectionId':d['collection']['id'],'title':f'Preview {index+1}','items':[{'id':str(uuid4()),'kind':'image','localPath':d['pins'][0]['items'][0]['localPath']}]})
(p/'references.papan').write_text(json.dumps(d))`, directory]);
  await run(python, ['-c', `import json,sys
from pathlib import Path
from uuid import uuid4
p=Path(sys.argv[1]);d=json.loads((p/'references.papan').read_text())
d['collection']['id']=str(uuid4());d['collection']['name']='Other references';d['pins']=d['pins'][:3]
for pin in d['pins']:
 pin['id']=str(uuid4());pin['collectionId']=d['collection']['id'];pin['title']='Other '+pin['title']
 for item in pin['items']:item['id']=str(uuid4())
(p/'other.papan').write_text(json.dumps(d))`, directory]);
  server = spawn(python, ['main.py', '--cpu', '--listen', '127.0.0.1', '--port', String(port), '--base-directory', base,
    '--user-directory', path.join(base, 'user'), '--database-url', 'sqlite:///:memory:', '--temp-directory', path.join(base, 'temp'),
    '--disable-auto-launch', '--disable-api-nodes', '--disable-all-custom-nodes', '--whitelist-custom-nodes', 'comfyui-papan'], { cwd: comfy });
  server.stdout.on('data', value => { log += value; }); server.stderr.on('data', value => { log += value; });
  let ready = false;
  for (let attempt = 0; attempt < 60; attempt++) {
    if (server.exitCode !== null) throw new Error(log);
    try { if ((await fetch(`${url}/system_stats`)).ok) { ready = true; break; } } catch {}
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  assert.ok(ready, 'Isolated ComfyUI did not start.');
  browser = await chromium.launch({ headless: true, ...(process.env.PAPAN_CHROMIUM ? { executablePath: process.env.PAPAN_CHROMIUM } : {}) });
  page = await browser.newPage({ viewport: { width: 1800, height: 1400 } });
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  async function openPage() {
    await page.goto(url);
    await page.waitForFunction(() => window.comfyAPI?.app?.app?.graph && window.LiteGraph?.registered_node_types?.PapanReferences);
    await page.waitForLoadState('networkidle');
    const close = page.getByRole('dialog').getByRole('button', { name: 'Close dialog', exact: true });
    if (await close.count()) await close.click();
    await page.evaluate(() => {
      const canvas = window.comfyAPI.app.app.canvas, render = canvas.renderLink;
      window.papanWireStarts = {};
      canvas.renderLink = function (ctx, start, end, link) {
        if (link) window.papanWireStarts[link.id] = Array.from(start);
        return render.apply(this, arguments);
      };
    });
  }
  await openPage();
  const ids = await page.evaluate(() => {
    const app = window.comfyAPI.app.app; app.graph.clear(); app.canvas.ds.scale = 1; app.canvas.ds.offset = [0, 0];
    const source = window.LiteGraph.createNode('PapanReferences'); source.pos = [80, 90]; app.graph.add(source);
    const image = window.LiteGraph.createNode('PreviewImage'), video = window.LiteGraph.createNode('GetVideoComponents'), seconds = window.LiteGraph.createNode('PapanDurationTest');
    for (const node of [image, video, seconds]) app.graph.add(node);
    image.pos = [1050, 200]; video.pos = [1050, 470]; seconds.pos = [1050, 740]; app.canvas.setDirty(true, true);
    const ordinary = window.LiteGraph.createNode('EmptyImage'), preview = window.LiteGraph.createNode('PreviewImage');
    ordinary.pos = [1050, 1110]; preview.pos = [1450, 1110];
    app.graph.add(ordinary); app.graph.add(preview); ordinary.connect(0, preview, 0);
    return { source: source.id, image: image.id, video: video.id, seconds: seconds.id, ordinary: ordinary.id };
  });
  await page.locator('.papan-file').setInputFiles(path.join(directory, 'references.papan'));
  await page.waitForFunction(() => document.querySelectorAll('.papan-card').length === 43);
  const grid = page.locator('.papan-grid');
  async function geometry() {
    return page.evaluate(id => {
      const app = window.comfyAPI.app.app, node = app.graph.getNodeById(id), grid = node.papanPanel.querySelector('.papan-grid');
      return { size: Array.from(node.size), gallery: grid.getBoundingClientRect().height, top: grid.scrollTop, scale: app.canvas.ds.scale, offset: Array.from(app.canvas.ds.offset) };
    }, ids.source);
  }
  async function assertWireAlignment(label) {
    await page.evaluate(() => { window.papanWireStarts = {}; window.comfyAPI.app.app.canvas.setDirty(true, true); });
    await page.waitForTimeout(150);
    const wires = await page.evaluate(id => {
      const app = window.comfyAPI.app.app, node = app.graph.getNodeById(id), bounds = app.canvas.canvas.getBoundingClientRect();
      return [...node.papanPanel.querySelectorAll('.papan-port')].flatMap(port => {
        const rect = port.getBoundingClientRect(), slot = Number(port.dataset.slot);
        return (node.outputs[slot].links || []).map(link => ({ slot, link,
          button: [(rect.x + rect.width / 2 - bounds.x) / app.canvas.ds.scale - app.canvas.ds.offset[0], (rect.y + rect.height / 2 - bounds.y) / app.canvas.ds.scale - app.canvas.ds.offset[1]],
          drawn: window.papanWireStarts[link] }));
      });
    }, ids.source);
    assert.ok(wires.length > 0, `${label}: connected references remain present.`);
    for (const wire of wires) {
      assert.ok(wire.drawn, `${label}: wire ${wire.link} is drawn.`);
      assert.ok(wire.drawn.every((value, axis) => Math.abs(value - wire.button[axis]) < 2), `${label}: slot ${wire.slot} starts at its connector: ${JSON.stringify(wire)}`);
    }
    const ordinary = await page.evaluate(id => {
      const node = window.comfyAPI.app.app.graph.getNodeById(id);
      return { expected: Array.from(node.getOutputPos(0)), drawn: window.papanWireStarts[node.outputs[0].links[0]] };
    }, ids.ordinary);
    assert.deepEqual(ordinary.drawn, ordinary.expected, `${label}: ordinary ComfyUI wires retain their native positions.`);
  }
  async function assertWireLayer(point) {
    const layer = await page.evaluate(point => {
      const canvas = window.comfyAPI.app.app.canvas.canvas, overlay = document.querySelector('.papan-wire-overlay');
      const bounds = overlay.getBoundingClientRect(), main = canvas.getBoundingClientRect();
      const x = Math.round((point.x - bounds.x) * overlay.width / bounds.width), y = Math.round((point.y - bounds.y) * overlay.height / bounds.height);
      const pixels = overlay.getContext('2d').getImageData(x - 4, y - 4, 9, 9).data;
      const passive = getComputedStyle(overlay).pointerEvents === 'none';
      overlay.style.pointerEvents = 'auto';
      const above = document.elementFromPoint(point.x, point.y) === overlay;
      overlay.style.pointerEvents = '';
      return { ink: [...pixels].some((value, index) => index % 4 === 3 && value > 0), passive, above,
        aligned: ['x', 'y', 'width', 'height'].every(key => Math.abs(bounds[key] - main[key]) < 1) };
    }, point);
    assert.ok(layer.ink && layer.above && layer.passive && layer.aligned, `Wire pixels appear above the preview without intercepting input: ${JSON.stringify(layer)}`);
  }
  async function connect(slot, target, checkDrag = false) {
    const start = await page.locator(`.papan-port[data-slot="${slot}"]`).boundingBox();
    const end = await page.evaluate(id => {
      const app = window.comfyAPI.app.app, point = app.graph.getNodeById(id).getInputPos(0), rect = app.canvas.canvas.getBoundingClientRect();
      return { x: rect.left + (point[0] + app.canvas.ds.offset[0]) * app.canvas.ds.scale, y: rect.top + (point[1] + app.canvas.ds.offset[1]) * app.canvas.ds.scale };
    }, target);
    await page.mouse.move(start.x + start.width / 2, start.y + start.height / 2); await page.mouse.down();
    if (checkDrag) {
      const gallery = await grid.boundingBox(), point = { x: gallery.x + 100, y: gallery.y + 60 };
      await page.mouse.move(point.x, point.y, { steps: 12 }); await page.waitForTimeout(150);
      await assertWireLayer(point);
      await page.screenshot({ path: `artifacts/papan-wire-layer-${vue ? 'vue' : 'canvas'}.png` });
    }
    await page.mouse.move(end.x, end.y, { steps: 12 }); await page.mouse.up();
    await page.waitForFunction(({ id, slot, source }) => {
      const app = window.comfyAPI.app.app, link = app.graph.links.get(app.graph.getNodeById(id).inputs[0].link);
      return link?.origin_id === source && link.origin_slot === slot;
    }, { id: target, slot, source: ids.source });
  }
  async function resize(dx, dy) {
    const start = vue ? await page.locator(`[data-node-id="${ids.source}"] [data-corner="SE"]`).boundingBox()
      : await page.evaluate(id => {
        const app = window.comfyAPI.app.app, node = app.graph.getNodeById(id), bounds = app.canvas.canvas.getBoundingClientRect();
        return { x: bounds.left + (node.pos[0] + node.size[0] + app.canvas.ds.offset[0] - 3) * app.canvas.ds.scale,
          y: bounds.top + (node.pos[1] + node.size[1] + app.canvas.ds.offset[1] - 3) * app.canvas.ds.scale, width: 0, height: 0 };
      }, ids.source);
    await page.mouse.move(start.x + start.width / 2, start.y + start.height / 2); await page.mouse.down();
    await page.mouse.move(start.x + start.width / 2 + dx, start.y + start.height / 2 + dy, { steps: 12 }); await page.mouse.up();
    await page.waitForTimeout(150);
  }
  const before = await geometry(), bounds = await grid.boundingBox();
  await page.mouse.move(bounds.x + 100, bounds.y + 70); await page.mouse.wheel(0, 350); await page.waitForTimeout(150);
  const wheeled = await geometry();
  await page.evaluate(() => { const app = window.comfyAPI.app.app; app.canvas.ds.scale = 1; app.canvas.ds.offset = [0, 0]; app.canvas.setDirty(true, true); });
  await resize(160, 170); const grown = await geometry();
  await resize(-220, -140); const smaller = await geometry();
  console.log(JSON.stringify({ renderer: vue ? 'Nodes 2.0' : 'canvas', before, wheeled, grown, smaller }));
  assert.ok(wheeled.top > before.top, 'The real mouse wheel scrolls the preview gallery.');
  assert.equal(wheeled.scale, before.scale, 'Gallery scrolling does not zoom the graph.');
  assert.deepEqual(wheeled.offset, before.offset, 'Gallery scrolling does not pan the graph.');
  assert.ok(grown.size[0] > before.size[0] + 120 && grown.gallery > before.gallery + 120, 'Dragging the node corner enlarges its width and preview area.');
  assert.ok(smaller.size[0] < grown.size[0] - 150 && smaller.gallery < grown.gallery - 100, 'Dragging inward shrinks the node and preview area.');
  await grid.evaluate(element => { element.scrollTop = element.scrollHeight; });
  const bottom = await geometry(), end = await grid.boundingBox();
  await page.mouse.move(end.x + 100, end.y + 50); await page.mouse.wheel(0, 350); await page.waitForTimeout(150);
  assert.deepEqual((await geometry()).offset, bottom.offset, 'The graph stays still when scrolling past the gallery end.');
  assert.equal((await geometry()).scale, bottom.scale, 'The graph does not zoom at the gallery end.');
  await grid.evaluate(element => { element.scrollTop = 0; });
  for (const slot of [0, 2]) await page.locator(`.papan-card[data-media-slot="${slot}"] .papan-select`).click();
  await page.waitForFunction(() => document.querySelector('.papan-seconds')?.textContent.includes('4.000'));
  const selected = await geometry(); assert.deepEqual(selected.size, smaller.size, 'Selections preserve the user size when outputs fit.');
  for (const [slot, target] of [[0, ids.image], [2, ids.video], [43, ids.seconds]]) {
    await connect(slot, target, slot === 0);
  }
  const output = await page.locator('.papan-port[data-slot="0"]').boundingBox(), gallery = await grid.boundingBox();
  await assertWireAlignment('Initial media connections');
  assert.ok(output.y >= gallery.y + gallery.height, 'Outputs remain below the preview gallery.');
  const videoPreview = await page.locator('.papan-card[data-media-slot="2"] video').boundingBox();
  await page.mouse.move(videoPreview.x + 80, Math.min(videoPreview.y + 30, gallery.y + gallery.height - 10));
  await page.mouse.wheel(0, 350); await page.waitForTimeout(150);
  assert.ok((await geometry()).top > 0, 'Wheel scrolling also works over a playing video preview.');
  assert.ok(Math.abs((await page.locator('.papan-port[data-slot="0"]').boundingBox()).y - output.y) < 2, 'Output positions stay fixed while scrolling.');
  await grid.evaluate(element => { element.scrollTop = 0; });
  await page.locator('.papan-card[data-media-slot="1"] .papan-select').click();
  await assertWireAlignment('Adding another reference');
  await page.locator('.papan-card[data-media-slot="0"] .papan-select').click();
  await connect(1, ids.image);
  await assertWireAlignment('Replacing the image reference');
  const firstBoard = await page.locator('.papan-board').inputValue();
  await page.locator('.papan-file').setInputFiles(path.join(directory, 'other.papan'));
  await page.waitForFunction(() => document.querySelectorAll('.papan-card').length === 3);
  await assertWireAlignment('Opening another board');
  const otherVideo = Number(await page.locator('.papan-card').nth(2).getAttribute('data-media-slot'));
  await page.locator(`.papan-card[data-media-slot="${otherVideo}"] .papan-select`).click();
  const otherSeconds = await page.evaluate(({ id, slot }) => JSON.parse(window.comfyAPI.app.app.graph.getNodeById(id).widgets.find(widget => widget.name === 'media_state').value).findIndex(entry => entry.kind === 'seconds' && entry.source_slot === slot), { id: ids.source, slot: otherVideo });
  await page.waitForFunction(slot => document.querySelector(`.papan-port[data-slot="${slot}"]`)?.textContent.includes('4.000'), otherSeconds);
  await connect(otherVideo, ids.video); await connect(otherSeconds, ids.seconds);
  await page.locator('.papan-output').filter({ has: page.locator('.papan-port[data-slot="2"]') }).locator('.papan-remove').click();
  await assertWireAlignment('Replacing the video and duration references');
  const otherImage = Number(await page.locator('.papan-card').nth(0).getAttribute('data-media-slot'));
  await page.locator(`.papan-card[data-media-slot="${otherImage}"] .papan-select`).click();
  await page.evaluate(({ source, slot }) => {
    const app = window.comfyAPI.app.app, target = window.LiteGraph.createNode('PapanMediaTest');
    target.pos = [1050, 1010];
    target.properties.minimax_h3_virtual_media_links = [{ source_id: source, source_slot: slot, source_type: 'IMAGE', media_type: 'image', order: 1 }];
    app.graph.add(target); app.canvas.setDirty(true, true);
  }, { source: ids.source, slot: otherImage });
  await page.waitForTimeout(150);
  const virtual = await page.locator(`.papan-port[data-slot="${otherImage}"]`).boundingBox();
  await assertWireLayer({ x: virtual.x + virtual.width / 2 + 3, y: virtual.y + virtual.height / 2 });
  await page.locator('.papan-board').selectOption(firstBoard);
  await page.waitForFunction(() => document.querySelectorAll('.papan-card').length === 43);
  await assertWireAlignment('Switching back to the original board');
  await page.evaluate(() => { const app = window.comfyAPI.app.app; app.canvas.ds.scale = 0.8; app.canvas.ds.offset = [60, 40]; app.canvas.setDirty(true, true); });
  await assertWireAlignment('Zooming and panning after switching references');
  const wire = await page.locator('.papan-port[data-slot="1"]').boundingBox();
  await assertWireLayer({ x: wire.x + wire.width / 2 + 3, y: wire.y + wire.height / 2 });
  const saved = await page.evaluate(() => JSON.parse(JSON.stringify(window.comfyAPI.app.app.graph.serialize())));
  await openPage();
  await page.evaluate(async saved => { await window.comfyAPI.app.app.loadGraphData(saved); }, saved);
  await page.waitForFunction(() => document.querySelectorAll('.papan-card').length === 43);
  await page.waitForTimeout(150);
  const restored = await geometry(); assert.ok(restored.size.every((value, axis) => Math.abs(value - selected.size[axis]) < 2), 'Saved workflows retain the resized dimensions.');
  const prompt = await page.evaluate(async () => (await window.comfyAPI.app.app.graphToPrompt()).output);
  assert.deepEqual(prompt[ids.seconds].inputs.seconds, [String(ids.source), otherSeconds]);
  await assertWireAlignment('Reloading references from a saved workflow');
  assert.deepEqual(errors, []);
  await page.screenshot({ path: `artifacts/papan-resize-scroll-${vue ? 'vue' : 'canvas'}.png` });
  await page.evaluate(() => { const app = window.comfyAPI.app.app; app.graph.clear(); app.canvas.setDirty(true, true); });
  await page.waitForTimeout(150);
  assert.ok(await page.locator('.papan-wire-overlay').evaluate(canvas => !canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data.some((value, index) => index % 4 === 3 && value > 0)), 'Clearing the workflow clears the wire layer.');
  console.log('Verified scrolling, resizing, IMAGE/VIDEO/FLOAT dragging, reference/board switching, exact drawn endpoints, wires above previews, zoom/pan alignment, and saved dimensions.');
} catch (error) {
  if (page) await page.screenshot({ path: 'artifacts/ui-failure.png' }).catch(() => {});
  await writeFile('artifacts/ui-server.log', log);
  throw error;
} finally {
  await browser?.close();
  if (server && server.exitCode === null) { server.kill('SIGTERM'); await new Promise(resolve => server.once('exit', resolve)); }
  await rm(directory, { recursive: true, force: true });
}
