import assert from 'node:assert/strict';
import { execFile, spawn } from 'node:child_process';
import { cp, mkdir, mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createInterface } from 'node:readline';
import { promisify } from 'node:util';
import { chromium } from 'playwright';

const comfy = process.env.COMFYUI_DIR, desktop = process.env.PAPAN_DESKTOP_DIR;
if (!comfy || !desktop) throw new Error('Set COMFYUI_DIR and PAPAN_DESKTOP_DIR for the isolated live-library check.');
const python = path.join(comfy, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const run = promisify(execFile), directory = await mkdtemp(path.join(tmpdir(), 'papan-live-ui-'));
const base = path.join(directory, 'comfy'), port = Number(process.env.PAPAN_TEST_PORT || 18439), url = `http://127.0.0.1:${port}`;
const vue = process.env.PAPAN_VUE_NODES === '1', renderer = vue ? 'vue' : 'canvas', checks = [], errors = [];
let server, library, browser, page, log = '', fixtureLog = '';
async function stopServer() {
  if (server && server.exitCode === null) { server.kill('SIGTERM'); await new Promise(resolve => server.once('exit', resolve)); }
}
async function startServer() {
  server = spawn(python, ['main.py', '--cpu', '--listen', '127.0.0.1', '--port', String(port), '--base-directory', base,
    '--user-directory', path.join(base, 'user'), '--database-url', 'sqlite:///:memory:', '--temp-directory', path.join(base, 'temp'),
    '--disable-auto-launch', '--disable-api-nodes', '--disable-all-custom-nodes', '--whitelist-custom-nodes', 'comfyui-papan'], { cwd: comfy });
  server.stdout.on('data', value => { log += value; }); server.stderr.on('data', value => { log += value; });
  for (let attempt = 0; attempt < 60; attempt++) {
    if (server.exitCode !== null) throw new Error(log);
    try { if ((await fetch(`${url}/system_stats`)).ok) return; } catch {}
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  throw new Error('Isolated ComfyUI did not start.');
}
try {
  try { await fetch(`${url}/system_stats`); throw new Error(`Test port ${port} is occupied.`); }
  catch (error) { if (!error.message.includes('fetch failed')) throw error; }
  await mkdir('artifacts', { recursive: true });
  await mkdir(path.join(base, 'custom_nodes'), { recursive: true });
  await mkdir(path.join(base, 'user', 'default'), { recursive: true });
  await writeFile(path.join(base, 'user', 'default', 'comfy.settings.json'), JSON.stringify({ 'Comfy.VueNodes.Enabled': vue }));
  if (process.env.PAPAN_EXTENSION_DIR) {
    await cp(process.env.PAPAN_EXTENSION_DIR, path.join(base, 'custom_nodes', 'comfyui-papan'), { recursive: true, filter: source => !['.git', 'node_modules', '__pycache__'].includes(path.basename(source)) });
  } else {
    await run(python, ['scripts/package.py', path.join(directory, 'dist')]);
    const version = (await readFile('VERSION', 'utf8')).trim();
    await run(python, ['-c', 'import sys,zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])', path.join(directory, 'dist', `comfyui-papan-v${version}.zip`), path.join(base, 'custom_nodes')]);
  }
  const init = path.join(base, 'custom_nodes', 'comfyui-papan', '__init__.py');
  await writeFile(init, await readFile(init, 'utf8') + `
class PapanLivePoseTest:
    @classmethod
    def INPUT_TYPES(cls): return {"required": {"video": ("VIDEO", {"forceInput": True}), "seconds": ("FLOAT", {"forceInput": True})}}
    RETURN_TYPES = ()
    FUNCTION = "load"
    OUTPUT_NODE = True
    def load(self, video, seconds):
        parts = video.get_components()
        return {"result": (), "ui": {"frames": [int(parts.images.shape[0])], "seconds": [float(seconds)], "audio": [parts.audio is not None]}}
NODE_CLASS_MAPPINGS["PapanLivePoseTest"] = PapanLivePoseTest
`);
  await run(python, ['-c', `import importlib.util,json,sys
from pathlib import Path
from uuid import uuid4
from PIL import Image
root=Path(sys.argv[1]);spec=importlib.util.spec_from_file_location('fixture_board','board.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
b=m.PapanBoard('tests/fixtures/pose-attachment.papan');b.unlock('pose-check-password');source=b.manifest['pins'][0]
image=root/'image.png';Image.new('RGBA',(24,12),(200,60,30,90)).save(image)
items=[{'id':str(uuid4()),'kind':'image','localPath':str(image)}]
for item in source['items']:
 _,contents,ext,_,_=b.media(source['id'],item['id'],True);file=root/(item['id']+ext);file.write_bytes(contents)
 items.append({'id':item['id'],'kind':'video','localPath':str(file),'duration':2.125,**({'poseFor':item['poseFor']} if item.get('poseFor') else {})})
collection={'id':str(uuid4()),'name':'Fixture'}
pins=[{'id':str(uuid4()),'collectionId':collection['id'],'title':'Live study','coverId':items[1]['id'],'items':items}]
for i in range(201):pins.append({'id':str(uuid4()),'collectionId':collection['id'],'title':f'Study {i+1}','items':[{'id':str(uuid4()),'kind':'image','localPath':str(image)}]})
(root/'references.papan').write_text(json.dumps({'format':'papan-collection','version':1,'collection':collection,'pins':pins}))`, directory]);
  library = spawn('node', ['tests/fixtures/live-library.mjs', desktop, path.join(directory, 'references.papan'), path.join(directory, 'library')]);
  library.stderr.on('data', data => { fixtureLog += data; });
  const lines = createInterface({ input: library.stdout })[Symbol.asyncIterator]();
  const description = JSON.parse((await lines.next()).value);
  async function command(action, data = {}) {
    library.stdin.write(JSON.stringify({ action, ...data }) + '\n');
    const result = JSON.parse((await lines.next()).value); assert.equal(result.ok, true, JSON.stringify(result)); return result;
  }
  await startServer();
  browser = await chromium.launch({ headless: true, ...(process.env.PAPAN_CHROMIUM ? { executablePath: process.env.PAPAN_CHROMIUM } : {}) });
  page = await browser.newPage({ viewport: { width: 1800, height: 1400 } });
  page.on('pageerror', error => errors.push(error.message));
  page.setDefaultTimeout(15000);
  async function openPage() {
    await page.goto(url);
    await page.waitForFunction(() => window.comfyAPI?.app?.app?.graph && window.LiteGraph?.registered_node_types?.PapanReferences);
    await page.waitForTimeout(500);
    const close = page.getByRole('dialog').getByRole('button', { name: 'Close dialog', exact: true });
    if (await close.count()) await close.click();
  }
  await openPage();
  const ids = await page.evaluate(checkDuration => {
    const app = window.comfyAPI.app.app; app.graph.clear(); app.canvas.ds.scale = 1; app.canvas.ds.offset = [0, 0];
    const source = window.LiteGraph.createNode('PapanReferences'), image = window.LiteGraph.createNode('PreviewImage'), pose = window.LiteGraph.createNode('PapanLivePoseTest'), protectedImage = window.LiteGraph.createNode('PreviewImage');
    source.pos = [80, 90]; source.setSize([620, 850]); image.pos = [1100, 150]; pose.pos = [1100, 450]; protectedImage.pos = [1100, 700];
    if (checkDuration) pose.properties.papan_video_duration = { video: 'video', seconds: 'seconds' };
    for (const node of [source, image, pose, protectedImage]) app.graph.add(node);
    return { source: source.id, image: image.id, pose: pose.id, protectedImage: protectedImage.id };
  }, process.env.PAPAN_CHECK_DURATION_FOLLOW === '1');
  await page.locator('.papan-connect').click();
  const dialog = page.getByRole('dialog', { name: 'Connect Papan', exact: true });
  await dialog.getByLabel('Connection copied from Papan', { exact: true }).fill('incomplete');
  await dialog.getByRole('button', { name: 'Connect', exact: true }).click();
  await dialog.getByRole('alert').filter({ hasText: 'complete connection' }).waitFor();
  await dialog.getByLabel('Connection copied from Papan', { exact: true }).fill(JSON.stringify(description.connection));
  await dialog.getByRole('button', { name: 'Connect', exact: true }).click();
  await dialog.getByRole('button', { name: 'Open collection', exact: true }).click();
  await page.waitForFunction(() => document.querySelectorAll('.papan-card').length === 203);
  const profiles = await (await fetch(`${url}/papan/connections`)).json(), connectionId = profiles.connections[0].id;
  assert.ok(!JSON.stringify(profiles).includes(description.connection.token));
  checks.push('explicit connection setup, incomplete-input error, app scopes and all 203 previews across native pagination');
  const slots = await page.evaluate(id => {
    const state = JSON.parse(window.comfyAPI.app.app.graph.getNodeById(id).widgets.find(widget => widget.name === 'media_state').value);
    const pose = state.findIndex(entry => entry.poseFor);
    return { image: state.findIndex(entry => entry.kind === 'image'), pose, video: state.findIndex(entry => entry.itemId === state[pose].poseFor), seconds: state.findIndex(entry => entry.kind === 'seconds' && entry.source_slot === pose) };
  }, ids.source);
  await page.locator(`[data-media-slot="${slots.video}"] .papan-picture`).click();
  await page.waitForFunction(() => document.querySelector('.papan-card video')?.readyState >= 2);
  assert.ok((await page.locator('.papan-card video').getAttribute('src')).includes(`/papan/connections/${connectionId}/media/`));
  assert.equal((await readdir(path.join(base, 'input', 'papan')).catch(() => [])).length, 0, 'Live playback streams without importing a ComfyUI input copy.');
  await page.locator(`[data-media-slot="${slots.image}"] .papan-select`).click();
  await page.locator('.papan-pose-select').click();
  async function connect(slot, target, input = 0) {
    const start = await page.locator(`.papan-port[data-slot="${slot}"] .papan-port-dot`).boundingBox();
    const end = await page.evaluate(({ target, input }) => {
      const app = window.comfyAPI.app.app, point = app.graph.getNodeById(target).getInputPos(input), rect = app.canvas.canvas.getBoundingClientRect();
      return { x: rect.left + (point[0] + app.canvas.ds.offset[0]) * app.canvas.ds.scale, y: rect.top + (point[1] + app.canvas.ds.offset[1]) * app.canvas.ds.scale };
    }, { target, input });
    await page.mouse.move(start.x + start.width / 2, start.y + start.height / 2); await page.mouse.down();
    await page.mouse.move(end.x, end.y, { steps: 12 }); await page.mouse.up();
    await page.waitForFunction(({ target, input, source, slot }) => {
      const app = window.comfyAPI.app.app, link = app.graph.links.get(app.graph.getNodeById(target).inputs[input].link);
      return link?.origin_id === source && link.origin_slot === slot;
    }, { target, input, source: ids.source, slot });
  }
  await connect(slots.image, ids.image); await connect(slots.pose, ids.pose); await connect(slots.seconds, ids.pose, 1);
  async function execute(prompt, expected = 'success') {
    prompt ||= await page.evaluate(async () => (await window.comfyAPI.app.app.graphToPrompt()).output);
    const response = await fetch(`${url}/prompt`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ prompt, client_id: 'papan-live-isolated-check' }) });
    const queued = await response.json(); assert.equal(response.status, 200, JSON.stringify(queued));
    for (let attempt = 0; attempt < 100; attempt++) {
      const history = (await (await fetch(`${url}/history/${queued.prompt_id}`)).json())[queued.prompt_id];
      if (history?.status?.completed || history?.status?.status_str === 'error') {
        assert.equal(history.status.status_str, expected, JSON.stringify(history.status)); return history;
      }
      await page.waitForTimeout(100);
    }
    throw new Error('The live workflow did not finish.');
  }
  const history = await execute();
  assert.deepEqual(history.outputs[ids.pose], { frames: [51], seconds: [2.125], audio: [false] });
  checks.push('streamed source playback without input copies; real IMAGE/VIDEO/FLOAT dragging and full 51-frame silent pose execution');
  async function selectCollection(index) {
    await page.locator('.papan-connect').click();
    await dialog.locator('.papan-connection select').selectOption(description.collections[index].id);
    await dialog.getByRole('button', { name: 'Open collection', exact: true }).click();
  }
  await selectCollection(1);
  await page.getByText('Unlock this collection in Papan. Its previews will appear here automatically.', { exact: true }).waitFor();
  assert.equal(await page.getByRole('dialog', { name: 'Unlock Papan board' }).count(), 0);
  await command('unlock');
  await page.waitForFunction(() => document.querySelectorAll('.papan-card').length === 2);
  const protectedSlot = await page.evaluate(({ id, collectionId }) => JSON.parse(window.comfyAPI.app.app.graph.getNodeById(id).widgets.find(widget => widget.name === 'media_state').value).findIndex(entry => entry.kind === 'image' && entry.collectionId === collectionId), { id: ids.source, collectionId: description.collections[1].id });
  assert.ok(protectedSlot >= 0);
  await page.locator(`[data-media-slot="${protectedSlot}"] .papan-select`).click();
  await connect(protectedSlot, ids.protectedImage);
  const protectedPrompt = await page.evaluate(async () => (await window.comfyAPI.app.app.graphToPrompt()).output);
  await execute(protectedPrompt);
  const saved = await page.evaluate(() => JSON.parse(JSON.stringify(window.comfyAPI.app.app.graph.serialize())));
  assert.ok(!JSON.stringify(saved).includes(description.connection.token));
  assert.ok(!JSON.stringify(saved).includes('live-test-password'));
  await page.locator('.papan-card').filter({ has: page.locator('.papan-play') }).locator('.papan-picture').click();
  await page.waitForFunction(() => document.querySelector('.papan-card video')?.readyState >= 2);
  await page.evaluate(() => { window.papanLockedPreview = document.querySelector('.papan-card video'); });
  await command('lock');
  await page.waitForFunction(() => document.querySelectorAll('.papan-card').length === 0 && document.querySelector('.papan-status').textContent.includes('Unlock'));
  assert.ok(await page.evaluate(() => window.papanLockedPreview.paused && !window.papanLockedPreview.getAttribute('src')), 'Locking stops and clears already buffered browser playback.');
  await execute(protectedPrompt, 'error');
  await command('unlock');
  await page.waitForFunction(() => document.querySelectorAll('.papan-card').length === 2);
  checks.push('protected collections unlock only in Papan; lock hides previews and blocks the exact previously cached prompt');
  await stopServer(); await startServer(); await openPage();
  await page.evaluate(async saved => { await window.comfyAPI.app.app.loadGraphData(saved); }, saved);
  await page.waitForFunction(() => document.querySelectorAll('.papan-card').length === 2);
  const links = await page.evaluate(ids => {
    const app = window.comfyAPI.app.app;
    return [app.graph.getNodeById(ids.image).inputs[0], app.graph.getNodeById(ids.pose).inputs[0], app.graph.getNodeById(ids.pose).inputs[1], app.graph.getNodeById(ids.protectedImage).inputs[0]].map(input => app.graph.links.get(input.link).origin_slot);
  }, ids);
  assert.deepEqual(links, [slots.image, slots.pose, slots.seconds, protectedSlot]);
  if (process.env.PAPAN_CHECK_DURATION_FOLLOW === '1') {
    await page.evaluate(id => window.comfyAPI.app.app.graph.getNodeById(id).disconnectInput(0), ids.pose);
    await page.waitForFunction(id => window.comfyAPI.app.app.graph.getNodeById(id).inputs[1].link == null, ids.pose);
    await page.evaluate(({ source, pose, slot }) => window.comfyAPI.app.app.graph.getNodeById(source).connect(slot, window.comfyAPI.app.app.graph.getNodeById(pose), 0), { source: ids.source, pose: ids.pose, slot: slots.pose });
    await page.waitForFunction(({ pose, source, seconds }) => {
      const app = window.comfyAPI.app.app, link = app.graph.links.get(app.graph.getNodeById(pose).inputs[1].link);
      return link?.origin_id === source && link.origin_slot === seconds;
    }, { pose: ids.pose, source: ids.source, seconds: slots.seconds });
    checks.push('installed duration-follow customization disconnects and restores the matching SECONDS wire with its VIDEO input');
  }
  await execute();
  await command('restart');
  await page.waitForFunction(() => document.querySelectorAll('.papan-card').length === 0 && document.querySelector('.papan-status').textContent.includes('Unlock'));
  await command('unlock'); await page.waitForFunction(() => document.querySelectorAll('.papan-card').length === 2);
  checks.push('ComfyUI restart reuses server-only credentials; workflow identities/wires survive; Papan restart reconnects with protected boards locked');
  await command('off'); await page.waitForFunction(() => document.querySelectorAll('.papan-card').length === 0);
  await command('on'); await page.waitForFunction(() => document.querySelectorAll('.papan-card').length === 2);
  await page.locator('.papan-board').selectOption({ label: 'Live references' });
  await page.waitForFunction(() => document.querySelectorAll('.papan-card').length === 203);
  await command('edit', { title: 'Updated in Papan' });
  await page.waitForFunction(() => document.querySelector('.papan-card-title')?.textContent === 'Updated in Papan');
  await command('remove'); await page.waitForFunction(() => document.querySelectorAll('.papan-card').length === 201);
  assert.equal(await page.locator(`.papan-port[data-slot="${slots.pose}"]`).isDisabled(), true);
  assert.deepEqual(await page.evaluate(ids => {
    const app = window.comfyAPI.app.app; return [app.graph.getNodeById(ids.pose).inputs[0], app.graph.getNodeById(ids.pose).inputs[1]].map(input => app.graph.links.get(input.link).origin_slot);
  }, ids), [slots.pose, slots.seconds]);
  checks.push('disable/re-enable reconnects; edits refresh automatically; removal preserves output slots and wires while disabling the missing reference');
  await command('revoke'); await page.waitForFunction(() => document.querySelector('.papan-status').textContent.includes('revoked'));
  const renewed = await command('rotate');
  await page.locator('.papan-connect').click();
  await dialog.getByRole('button', { name: 'Update connection', exact: true }).click();
  await dialog.getByLabel('Connection copied from Papan', { exact: true }).fill(JSON.stringify(renewed.connection));
  await dialog.getByRole('button', { name: 'Connect', exact: true }).click();
  await dialog.getByRole('button', { name: 'Open collection', exact: true }).click();
  await page.waitForFunction(() => document.querySelectorAll('.papan-card').length === 201);
  assert.equal((await (await fetch(`${url}/papan/connections`)).json()).connections[0].id, connectionId);
  await page.locator('.papan-connect').click();
  await page.setViewportSize({ width: 380, height: 640 });
  assert.ok(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth && element.getBoundingClientRect().height <= innerHeight));
  await page.screenshot({ path: `artifacts/papan-live-dialog-${renderer}.png` });
  await dialog.getByRole('button', { name: 'Disconnect', exact: true }).click();
  assert.deepEqual((await (await fetch(`${url}/papan/connections`)).json()).connections, []);
  await dialog.getByLabel('Connection copied from Papan', { exact: true }).fill(JSON.stringify(renewed.connection));
  await dialog.getByRole('button', { name: 'Connect', exact: true }).click();
  await dialog.getByRole('button', { name: 'Open collection', exact: true }).click();
  await page.waitForFunction(() => document.querySelectorAll('.papan-card').length === 201);
  assert.equal((await (await fetch(`${url}/papan/connections`)).json()).connections[0].id, connectionId);
  await page.locator('.papan-connect').click();
  await dialog.getByRole('button', { name: 'Disconnect', exact: true }).click();
  await page.keyboard.press('Escape');
  await page.setViewportSize({ width: 1800, height: 1400 });
  await page.screenshot({ path: `artifacts/papan-live-${renderer}.png` });
  assert.deepEqual(errors, []);
  checks.push('revoked key requires renewal; updating and reconnecting preserve profile/workflow identities; disconnect removes credentials; keyboard dismissal and narrow dialog fit');
  await writeFile(`artifacts/live-ui-${renderer}.json`, JSON.stringify({ status: 'passed', renderer, checks }, null, 2) + '\n');
  console.log(JSON.stringify({ status: 'passed', renderer, checks }, null, 2));
} catch (error) {
  await page?.screenshot({ path: `artifacts/live-ui-failure-${renderer}.png` }).catch(() => {});
  throw error;
} finally {
  await writeFile(`artifacts/live-ui-server-${renderer}.log`, log + fixtureLog);
  await browser?.close(); await stopServer();
  if (library && library.exitCode === null) { library.stdin.end(); await new Promise(resolve => library.once('exit', resolve)); }
  await rm(directory, { recursive: true, force: true });
}
