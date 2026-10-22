// Раздел «Доступ из сети» (prototypes/desk-server.js): куда смотреть после перезапуска (новый порт, HTTPS, другой
// компьютер), как страница узнаёт, что сервер перезапустился (другой X-Desk-Instance) или вернул прежние настройки,
// и предупреждения формы, которые не дают запереть себя снаружи.
//   node tests/js/desk_server.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const SRC = fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', 'desk-server.js'), 'utf8');

const ctx = { window: {}, document: {}, console, setTimeout, clearTimeout };
vm.createContext(ctx);
vm.runInContext(SRC, ctx);
const T = ctx.window.DeskServer._test;
const plain = x => JSON.parse(JSON.stringify(x));
let n = 0;
const t = async (name, f) => { await f(); n++; console.log('ok', name); };
const LOCAL = { protocol: 'http:', hostname: '127.0.0.1', port: '8100', origin: 'http://127.0.0.1:8100' };
const REMOTE = { protocol: 'https:', hostname: 'desk-pc', port: '8100', origin: 'https://desk-pc:8100' };
const resp = (ok, inst) => ({ ok, headers: { get: k => (k.toLowerCase() === 'x-desk-instance' ? inst : null) } });

(async () => {
  await t('адрес после перезапуска: тот же, новый порт и HTTPS, другой компьютер, доступ закрыт', () => {
    assert.deepStrictEqual(plain(T.targetOrigin({ lan: false, bind_host: '127.0.0.1', port: 8100, https: false }, LOCAL)),
      { origin: 'http://127.0.0.1:8100', same: true, lost: false });
    assert.deepStrictEqual(plain(T.targetOrigin({ lan: true, bind_host: '0.0.0.0', port: 8443, https: true }, LOCAL)),
      { origin: 'https://127.0.0.1:8443', same: false, lost: false });
    assert.strictEqual(T.targetOrigin({ lan: true, bind_host: '0.0.0.0', port: 8100, https: true }, REMOTE).same, true);
    assert.strictEqual(T.targetOrigin({ lan: true, bind_host: '10.1.2.3', port: 8100, https: true }, REMOTE).origin, 'https://10.1.2.3:8100');
    assert.strictEqual(T.targetOrigin({ lan: false, bind_host: '127.0.0.1', port: 8100, https: true }, REMOTE).lost, true);
    assert.ok(T.isLoopHost('localhost') && T.isLoopHost('127.0.0.2') && !T.isLoopHost('desk-pc'));
  });

  await t('ожидание: прежний процесс, новый, новый адрес, откат', async () => {
    const same = { origin: 'http://127.0.0.1:8100', same: true, instance: 'old' };
    assert.strictEqual(await T.probe(same, () => Promise.resolve(resp(true, 'old'))), 'old');
    assert.strictEqual(await T.probe(same, () => Promise.reject(new TypeError('down'))), 'wait');
    assert.strictEqual(await T.probe(same, () => Promise.resolve(resp(true, 'new'))), 'reload');
    const moved = { origin: 'https://127.0.0.1:8443', same: false, instance: 'old' };
    const seen = [];
    assert.strictEqual(await T.probe(moved, (u, o) => { seen.push([u, o.mode]); return Promise.resolve({ type: 'opaque' }); }), 'go');
    assert.deepStrictEqual(seen, [['https://127.0.0.1:8443/health/live', 'no-cors']]);
    const down = u => u.indexOf('https://') === 0 ? Promise.reject(new TypeError('cert')) : null;
    assert.strictEqual(await T.probe(moved, u => down(u) || Promise.resolve(resp(true, 'new'))), 'back');  // прежние настройки
    assert.strictEqual(await T.probe(moved, u => down(u) || Promise.resolve(resp(true, 'old'))), 'old');
    assert.strictEqual(await T.probe(moved, u => down(u) || Promise.reject(new TypeError('down'))), 'wait');
  });

  const state = (over) => Object.assign({ configured: { lan: false, auth: false, https: false, bind_host: '127.0.0.1', port: 8100, admin_from_network: true },
    admins: ['boss'], client: { loopback: true, may_change_network: true } }, over || {});
  const form = (over) => Object.assign({ lan: true, https: true, port: 8100, host: '0.0.0.0', regen: false, afn: true, authOff: false }, over || {});

  await t('предупреждения: без администратора — нельзя; вход включится; HTTP; с другого компьютера', () => {
    let w = T.warnings(state({ admins: [] }), form(), true);
    assert.ok(w.block && /администратор/.test(w.list[0].text));
    w = T.warnings(state({ admins: null }), form(), true);
    assert.ok(w.block && /База недоступна/.test(w.list[0].text));
    w = T.warnings(state(), form(), true);
    assert.ok(!w.block && w.list.some(x => /boss/.test(x.text)) && w.list.some(x => /новый сертификат/.test(x.text)));
    w = T.warnings(state(), form({ https: false }), true);
    assert.ok(w.list.some(x => /открытым текстом/.test(x.text)));
    w = T.warnings(state({ client: { loopback: false, may_change_network: true } }), form({ lan: false }), false);
    assert.ok(!w.block && w.list.some(x => /больше не откроется/.test(x.text)));
    w = T.warnings(state({ client: { loopback: false, may_change_network: false } }), form(), false);
    assert.ok(w.block);
    assert.ok(T.warnings(state(), form({ port: 70000 }), true).block);
    assert.ok(T.warnings(state(), form({ host: '' }), true).block);
  });

  await t('изменения против сохранённого', () => {
    const c = state().configured;
    assert.ok(!T.changed(c, form({ lan: false })));
    assert.ok(T.changed(c, form({ lan: false, port: 8443 })));
    assert.ok(T.changed(c, form()));
    const on = { lan: true, auth: true, https: true, bind_host: '0.0.0.0', port: 8100, admin_from_network: true };
    assert.ok(!T.changed(on, form()));
    assert.ok(T.changed(on, form({ regen: true })) && T.changed(on, form({ https: false })) && T.changed(on, form({ afn: false })));
    assert.ok(T.changed(on, form({ lan: false })));
  });

  console.log(n + ' tests passed');
})().catch(e => { console.error(e); process.exit(1); });
