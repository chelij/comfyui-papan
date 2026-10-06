import { createCipheriv, createDecipheriv, createHash, randomBytes, scrypt } from 'node:crypto';
import { promisify } from 'node:util';
import { open, stat, copyFile, mkdir } from 'node:fs/promises';
import { constants } from 'node:fs';
import path from 'node:path';

// Version 1: fixed scrypt parameters; AES-256-GCM for the key envelope,
// manifest, and independently authenticated 1 MiB media chunks. No plaintext
// metadata or media is written to the vault, including while it is unlocked.
const MAGIC = Buffer.from('PAPANENC'), HEADER = 100, CHUNK = 1024 * 1024;
const derive = promisify(scrypt);
export function seal(bytes, key, context) {
  const nonce = randomBytes(12), cipher = createCipheriv('aes-256-gcm', key, nonce);
  cipher.setAAD(Buffer.from(context));
  const encrypted = Buffer.concat([cipher.update(bytes), cipher.final()]);
  return Buffer.concat([nonce, cipher.getAuthTag(), encrypted]);
}
export function unseal(bytes, key, context) {
  if (bytes.length < 28) throw new Error('Damaged encrypted data.');
  const cipher = createDecipheriv('aes-256-gcm', key, bytes.subarray(0, 12));
  cipher.setAAD(Buffer.from(context)); cipher.setAuthTag(bytes.subarray(12, 28));
  return Buffer.concat([cipher.update(bytes.subarray(28)), cipher.final()]);
}
export function checkPassword(password) {
  if (typeof password !== 'string' || password.length < 8 || password.length > 1024) throw new Error('Use a password between 8 and 1,024 characters.');
}
async function passwordKey(password, salt) {
  if (typeof password !== 'string' || password.length > 1024) throw new Error('Invalid password.');
  return derive(password, salt, 32, { N: 131072, r: 8, p: 1, maxmem: 256 * 1024 * 1024 });
}
export async function wrapKey(password, key = randomBytes(32)) {
  checkPassword(password);
  const salt = randomBytes(16), derived = await passwordKey(password, salt);
  try { return { key, envelope: Buffer.concat([salt, seal(key, derived, Buffer.concat([MAGIC, salt]))]) }; }
  finally { derived.fill(0); }
}
async function readExactly(handle, length, position) {
  const bytes = Buffer.alloc(length);
  for (let offset = 0; offset < length;) {
    const result = await handle.read(bytes, offset, length - offset, position + offset);
    if (!result.bytesRead) throw new Error('The encrypted collection is incomplete.');
    offset += result.bytesRead;
  }
  return bytes;
}
async function writeAll(handle, bytes, position) {
  for (let offset = 0; offset < bytes.length;) {
    const { bytesWritten } = await handle.write(bytes, offset, bytes.length - offset, position + offset);
    if (!bytesWritten) throw new Error('Could not finish writing the encrypted collection.');
    offset += bytesWritten;
  }
}
export async function isVault(file) {
  const handle = await open(file, 'r');
  try { const bytes = Buffer.alloc(8); await handle.read(bytes, 0, 8, 0); return bytes.equals(MAGIC); }
  finally { await handle.close(); }
}
export async function vaultHeader(file) {
  const handle = await open(file, 'r');
  try {
    const header = await readExactly(handle, HEADER, 0), size = (await handle.stat()).size;
    if (!header.subarray(0, 8).equals(MAGIC)) throw new Error('Unsupported encrypted collection.');
    const offset = Number(header.readBigUInt64BE(84)), length = Number(header.readBigUInt64BE(92));
    if (!Number.isSafeInteger(offset) || offset < HEADER || length < 28 || length > 64 * 1024 * 1024 || offset + length !== size) throw new Error('Invalid encrypted collection size.');
    return { header, envelope: header.subarray(8, 84), offset, length, revision: createHash('sha256').update(header).update(await readExactly(handle, 28, offset)).digest('hex') };
  } finally { await handle.close(); }
}
export async function unlockVault(file, password) {
  const info = await vaultHeader(file), salt = info.envelope.subarray(0, 16);
  const derived = await passwordKey(password, salt);
  let key;
  try {
    key = unseal(info.envelope.subarray(16), derived, Buffer.concat([MAGIC, salt]));
    const manifest = await readVault(file, key);
    return { key, envelope: Buffer.from(info.envelope), file, manifest };
  } catch { key?.fill(0); throw new Error('Incorrect password or damaged encrypted collection.'); }
  finally { derived.fill(0); }
}
export async function readVault(file, key) {
  const info = await vaultHeader(file), handle = await open(file, 'r');
  try {
    const bytes = unseal(await readExactly(handle, info.length, info.offset), key, info.header);
    const manifest = JSON.parse(bytes.toString('utf8'));
    if (manifest?.format !== 'papan-encrypted' || manifest.version !== 1 || !manifest.collection || !Array.isArray(manifest.pins) || !Array.isArray(manifest.trash)) throw new Error('Invalid encrypted collection metadata.');
    return manifest;
  } finally { await handle.close(); }
}
export function validEntry(entry) {
  if (!entry || !/^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(entry.id) || !Number.isSafeInteger(entry.offset) || entry.offset < HEADER || !Number.isSafeInteger(entry.size) || entry.size < 1 || entry.size > 512 * 1024 * 1024 || !/^\.[a-z0-9]{1,8}$/.test(entry.ext)) throw new Error('Invalid encrypted media reference.');
  return entry;
}
export async function* vaultRange(file, key, entry, start = 0, end = entry.size - 1, signal) {
  validEntry(entry);
  if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 0 || end >= entry.size || start > end) throw new Error('Invalid encrypted media range.');
  const handle = await open(file, 'r');
  try {
    const info = await vaultHeader(file);
    if (entry.offset + entry.size + Math.ceil(entry.size / CHUNK) * 28 > info.offset) throw new Error('Encrypted media is outside the collection.');
    for (let index = Math.floor(start / CHUNK); index <= Math.floor(end / CHUNK); index++) {
      signal?.throwIfAborted();
      const length = Math.min(CHUNK, entry.size - index * CHUNK);
      const bytes = await readExactly(handle, length + 28, entry.offset + index * (CHUNK + 28));
      const decoded = unseal(bytes, key, `${entry.id}:${index}:${entry.size}`);
      yield decoded.subarray(Math.max(0, start - index * CHUNK), Math.min(length, end - index * CHUNK + 1));
    }
  } finally { await handle.close(); }
}
export async function beginVault(file, session) {
  await mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
  const previous = !session.rotate && session.file;
  if (previous) await copyFile(previous, file, constants.COPYFILE_FICLONE);
  const handle = await open(file, previous ? 'r+' : 'wx', 0o600);
  let position = previous ? (await handle.stat()).size : HEADER;
  return {
    async add(id, source, ext) {
      const size = source.entry ? validEntry(source.entry).size : (await stat(source.file)).size;
      const entry = validEntry({ id, offset: position, size, ext });
      const input = source.entry ? null : await open(source.file, 'r');
      try {
        for (let index = 0; index * CHUNK < size; index++) {
          const start = index * CHUNK, length = Math.min(CHUNK, size - start);
          const bytes = source.entry ? Buffer.concat(await Array.fromAsync(vaultRange(source.file, source.key, source.entry, start, start + length - 1))) : await readExactly(input, length, start);
          const encrypted = seal(bytes, session.key, `${id}:${index}:${size}`);
          await writeAll(handle, encrypted, position); position += encrypted.length;
        }
        return entry;
      } finally { await input?.close(); }
    },
    async finish(manifest) {
      const bytes = Buffer.from(JSON.stringify(manifest));
      if (bytes.length + 28 > 64 * 1024 * 1024) throw new Error('This collection has too much metadata.');
      const header = Buffer.alloc(HEADER); MAGIC.copy(header); session.envelope.copy(header, 8);
      header.writeBigUInt64BE(BigInt(position), 84); header.writeBigUInt64BE(BigInt(bytes.length + 28), 92);
      const encrypted = seal(bytes, session.key, header);
      await writeAll(handle, encrypted, position);
      await writeAll(handle, header, 0); await handle.sync();
    },
    close: () => handle.close(),
  };
}
