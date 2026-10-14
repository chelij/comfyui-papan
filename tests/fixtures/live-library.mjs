import { mkdir, readFile, copyFile } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { createInterface } from 'node:readline';

const [desktop, manifestFile, root] = process.argv.slice(2);
if (!desktop || !manifestFile || !root) throw new Error('Provide the Papan source, synthetic manifest and isolated library directory.');
const { openLibrary, newCollection } = await import(pathToFileURL(path.join(desktop, 'src/library.js')));
const { openLibraryService } = await import(pathToFileURL(path.join(desktop, 'src/library-service.js')));
await mkdir(root, { recursive: true });
let library = await openLibrary(root), service;
const manifest = JSON.parse(await readFile(manifestFile, 'utf8'));
const collections = [newCollection('Live references'), newCollection('Protected references'), newCollection('Not shared')];
const pins = [];
for (const [index, collection] of collections.entries()) {
  for (const source of index ? manifest.pins.slice(0, 1) : manifest.pins) {
    const folder = randomUUID(), identities = new Map(source.items.map(item => [item.id, randomUUID()]));
    await mkdir(path.join(root, 'media', folder));
    const pin = { engine: 'page', sourceUrl: 'https://example.com/papan-test', ...source, id: randomUUID(), collectionId: collection.id, folder, offline: true, coverId: identities.get(source.coverId) || identities.get(source.items[0].id), items: [] };
    for (const item of source.items) {
      const copy = { ...item, id: identities.get(item.id), ...(item.poseFor ? { poseFor: identities.get(item.poseFor) } : {}) };
      for (const [field, saved] of [['localPath', 'localFile'], ['previewPath', 'previewFile']]) {
        if (item[field]) {
          const relative = `${folder}/${copy.id}-${saved}${path.extname(item[field])}`;
          await copyFile(item[field], path.join(root, 'media', relative)); copy[saved] = relative;
        }
        delete copy[field];
      }
      pin.items.push(copy);
    }
    pins.push(pin);
  }
}
await library.mutate(draft => { draft.collections.push(...collections); draft.pins.push(...pins); });
await library.mutate(draft => library.protection.setPassword(draft, collections[1].id, 'live-test-password'));
service = await openLibraryService(root, { library, port: 0 });
await service.configure({ enabled: true, port: 0 });
const port = service.snapshot().port;
await service.configure({ enabled: true, port });
let connection = await service.grant({ name: 'ComfyUI fixture', collectionIds: collections.slice(0, 2).map(collection => collection.id) });
await library.mutate(draft => library.protection.lock(draft, collections[1].id));
const describe = () => ({ connection: { version: 1, endpoint: connection.endpoint, token: connection.token }, collections, pins: pins.map(pin => ({ id: pin.id, collectionId: pin.collectionId, items: pin.items.map(item => ({ id: item.id, kind: item.kind, poseFor: item.poseFor })) })) });
process.stdout.write(JSON.stringify(describe()) + '\n');
let commands = Promise.resolve();
const input = createInterface({ input: process.stdin });
input.on('line', line => {
  commands = commands.then(async () => {
    try {
      const data = JSON.parse(line);
      if (data.action === 'unlock') await library.mutate(draft => library.protection.unlock(draft, collections[1].id, 'live-test-password'));
      else if (data.action === 'lock') await library.mutate(draft => library.protection.lock(draft, collections[1].id));
      else if (data.action === 'edit') await library.mutate(draft => { draft.pins.find(pin => pin.id === pins[0].id).title = data.title || 'Updated in Papan'; });
      else if (data.action === 'remove') await library.mutate(draft => { draft.pins = draft.pins.filter(pin => pin.id !== pins[0].id); });
      else if (data.action === 'scope') await service.updateClient({ id: connection.clientId, name: 'ComfyUI fixture', collectionIds: [collections[0].id] });
      else if (data.action === 'revoke') await service.revoke(connection.clientId);
      else if (data.action === 'rotate') {
        connection = await service.grant({ name: 'ComfyUI renewed fixture', collectionIds: [collections[0].id] });
      } else if (data.action === 'off') await service.configure({ enabled: false, port });
      else if (data.action === 'on') await service.configure({ enabled: true, port });
      else if (data.action === 'restart') {
        await service.stop(); library.protection.clear(); library = await openLibrary(root); service = await openLibraryService(root, { library });
      } else throw new Error('Unknown fixture command.');
      process.stdout.write(JSON.stringify({ ok: true, ...(data.action === 'rotate' ? describe() : {}) }) + '\n');
    } catch (error) { process.stdout.write(JSON.stringify({ error: error.message }) + '\n'); }
  });
});
input.on('close', () => { void commands.then(async () => { await service.stop(); library.protection.clear(); }); });
process.on('SIGTERM', () => input.close());
