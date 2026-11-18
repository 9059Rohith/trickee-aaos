'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function worker({ offline = false, cacheControl = '', quotaExceeded = false } = {}) {
  const handlers = new Map();
  const deleted = [];
  const stored = [];
  const pending = [];
  const cached = new Response('offline shell');
  const cache = {
    addAll: async () => {},
    put: async request => {
      if (quotaExceeded) throw new Error('storage quota exceeded');
      stored.push(request.url);
    },
    match: async () => cached,
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../driver-console/sw.js'), 'utf8'), {
    URL,
    self: {
      location: new URL('https://fleet.trickee.test/driver/sw.js'),
      addEventListener: (name, handler) => handlers.set(name, handler),
      skipWaiting: async () => {},
      clients: { claim: async () => {} },
    },
    caches: {
      open: async () => cache,
      keys: async () => ['another-app-cache', 'trickee-driver-shell-v1'],
      delete: async name => { deleted.push(name); },
      match: async () => new Response('unrelated application cache'),
    },
    fetch: async () => {
      if (offline) throw new Error('offline');
      return new Response('online shell', { headers: { 'Cache-Control': cacheControl } });
    },
  });
  return {
    deleted, stored,
    async activate() {
      handlers.get('activate')({ waitUntil: promise => pending.push(promise) });
      await Promise.all(pending);
    },
    async request(url) {
      let response;
      handlers.get('fetch')({
        request: new Request(url),
        respondWith: promise => { response = promise; },
        waitUntil: promise => pending.push(promise),
      });
      const result = response ? await response : undefined;
      await Promise.all(pending);
      return result;
    },
  };
}

test('activation removes only obsolete Trickee driver caches', async () => {
  const app = worker();
  await app.activate();
  assert.deepEqual(app.deleted, ['trickee-driver-shell-v1']);
});

test('unrelated, private, query-bearing and external resources bypass shell caching', async () => {
  for (const resource of [
    'https://tiles.example/1/2/3.png',
    'https://fleet.trickee.test/api/v1/driver/me',
    'https://fleet.trickee.test/account-access?token=private',
    'https://fleet.trickee.test/driver?token=private',
    'https://fleet.trickee.test/unrelated-app',
  ]) {
    const app = worker();
    assert.equal(await app.request(resource), undefined, resource);
    assert.deepEqual(app.stored, []);
  }
});

test('the driver shell still loads offline', async () => {
  const app = worker({ offline: true });
  assert.equal(await (await app.request('https://fleet.trickee.test/driver')).text(), 'offline shell');
});

test('network responses marked no-store are never persisted', async () => {
  const app = worker({ cacheControl: 'private, no-store' });
  assert.equal(await (await app.request('https://fleet.trickee.test/driver')).text(), 'online shell');
  assert.deepEqual(app.stored, []);
});

test('successful static shell requests remain cacheable', async () => {
  const app = worker();
  assert.equal(await (await app.request('https://fleet.trickee.test/driver/app.js')).text(), 'online shell');
  assert.deepEqual(app.stored, ['https://fleet.trickee.test/driver/app.js']);
});

test('a full cache does not break a successful network response', async () => {
  const app = worker({ quotaExceeded: true });
  assert.equal(await (await app.request('https://fleet.trickee.test/driver')).text(), 'online shell');
});
