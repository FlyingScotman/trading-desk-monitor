// «Ручные рыночные данные» (C22, TDM#652), логика без браузера: разбор введённого (par-спред в б.п., доля возмещения в %,
// валюта, имя), черновик строки и признак «изменено», строки таблицы (кривые даты, CDS-позиции прогона, добавленные
// имена), что копирование даст, права на правку по /api/v1/auth/state, вызовы к серверу (адрес, метод, JSON, обрыв связи)
// и демо в памяти страницы с теми же правилами, что у сервера; сроки, доля возмещения и верхняя граница — те же, что в
// backend desk/marketdata/manual_cds.py; экран в каталоге и без фильтра шапки.
//   node --test tests/js/manual_md_652.test.js
'use strict';
const test = require('node:test'), assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const ROOT = path.join(__dirname, '..', '..');
const MD = require(path.join(ROOT, 'prototypes', 'concepts', 'lib', 'manual-md.js'));
const PY = fs.readFileSync(path.join(ROOT, 'backend', 'src', 'desk', 'marketdata', 'manual_cds.py'), 'utf8');
const TENORS = ['3M', '6M', '1Y', '2Y', '3Y', '5Y', '7Y', '10Y'];

test('сроки, доля возмещения, валюта и верхняя граница — те же, что у сервера (manual_cds.py)', () => {
  const py = /^TENORS = \(([^)]*)\)/m.exec(PY)[1].match(/"([0-9]+[MY])"/g).map(s => s.replace(/"/g, ''));
  assert.deepEqual(MD.TENORS, py);
  assert.deepEqual(MD.TENORS, TENORS);
  assert.equal(MD.DEFAULTS.recovery, Number(/^DEFAULT_RECOVERY = ([0-9.]+)/m.exec(PY)[1]));
  assert.equal(MD.DEFAULTS.ccy, /^DEFAULT_CCY = "([A-Z]{3})"/m.exec(PY)[1]);
  assert.equal(MD.DEFAULTS.maxSpread, Number(/^MAX_SPREAD_BP = ([0-9.]+)/m.exec(PY)[1]));
  assert.equal(MD.DEFAULTS.maxName, Number(/^MAX_NAME = ([0-9]+)/m.exec(PY)[1]));
  assert.ok(/par/.test(MD.CONVENTION) && /25 %/.test(MD.CONVENTION) && /USD/.test(MD.CONVENTION) && /19\.11\.2026/.test(MD.CONVENTION));
});

test('par-спред: число с точкой или запятой, пусто — срок не задан, отрицательное и выше границы — отказ', () => {
  assert.deepEqual(MD.parseSpread(''), { ok: true, value: null });
  assert.deepEqual(MD.parseSpread('  '), { ok: true, value: null });
  assert.equal(MD.parseSpread('150').value, 150);
  assert.equal(MD.parseSpread('150,5').value, 150.5);
  assert.equal(MD.parseSpread(' 1 250.25 ').value, 1250.25);
  assert.equal(MD.parseSpread('0').value, 0);
  assert.equal(MD.parseSpread('10000').value, 10000);
  for (const bad of ['-1', '10000,01', 'abc', '1e3', '1,2,3', '--5', '12 б.п.', 'NaN', 'Infinity']) {
    const r = MD.parseSpread(bad, '5Y');
    assert.equal(r.ok, false, bad);
    assert.ok(r.error.startsWith('5Y: '), bad + ': ' + r.error);
  }
});

test('доля возмещения вводится в процентах и хранится долей; пусто — по умолчанию', () => {
  assert.deepEqual(MD.parseRecovery(''), { ok: true, value: null });
  assert.equal(MD.parseRecovery('25').value, 0.25);
  assert.equal(MD.parseRecovery('40 %').value, 0.4);
  assert.equal(MD.parseRecovery('33,33').value, 0.3333);
  assert.equal(MD.parseRecovery('0').value, 0);
  assert.equal(MD.parseRecovery('100').value, 1);
  for (const bad of ['101', '-5', 'много', '0,25 %%']) assert.equal(MD.parseRecovery(bad).ok, false, bad);
  assert.equal(MD.fmtPct(0.25), '25');
  assert.equal(MD.fmtPct(0.3333), '33,33');
  assert.equal(MD.fmtSpread(140.5), '140,5');
  assert.equal(MD.fmtSpread(140.1234), '140,1234');
  assert.equal(MD.fmtSpread(null), '');
});

test('валюта и имя: три латинские буквы; имя без служебных символов, пробелы в один, не длиннее 120', () => {
  assert.deepEqual(MD.parseCcy(' eur '), { ok: true, value: 'EUR' });
  assert.deepEqual(MD.parseCcy(''), { ok: true, value: null });
  for (const bad of ['US', 'РУБ', 'USDD', 'U$D']) assert.equal(MD.parseCcy(bad).ok, false, bad);
  const n = MD.parseName('  Эмитент   А ');
  assert.deepEqual([n.ok, n.name, n.key], [true, 'Эмитент А', 'эмитент а']);
  assert.equal(MD.parseName('   ').ok, false);
  assert.equal(MD.parseName('a\u0000b').ok, false);
  assert.equal(MD.parseName('x'.repeat(121)).ok, false);
  assert.equal(MD.parseName('x'.repeat(120)).ok, true);
  assert.equal(MD.nameKey('ЭМИТЕНТ  А'), MD.nameKey(' эмитент а '));
  assert.equal(MD.nameKey('Ｅｍｉｔ A'), 'emit a');
});

const CURVE = { rev_id: 1, name_key: 'эмитент а', name: 'Эмитент А', valid_from: '2026-11-16', entered_on_date: false, cleared: false,
  spreads_bp: { '3M': null, '6M': null, '1Y': 140.5, '2Y': null, '3Y': 240, '5Y': 300, '7Y': null, '10Y': null }, recovery: 0.4, ccy: 'EUR',
  changed_by: 'tom', changed_at: '2026-11-19T09:30:00+00:00', manual: true };

test('черновик строки: тексты полей из кривой, тело PUT из текстов, «изменено» — по смыслу, не по буквам', () => {
  const d = MD.draftOf(CURVE);
  assert.deepEqual(d.spreads, { '3M': '', '6M': '', '1Y': '140,5', '2Y': '', '3Y': '240', '5Y': '300', '7Y': '', '10Y': '' });
  assert.deepEqual([d.recovery, d.ccy], ['40', 'EUR']);
  const r = MD.readDraft('Эмитент А', '2026-11-18', d);
  assert.equal(r.ok, true);
  assert.deepEqual(r.body, { name: 'Эмитент А', date: '2026-11-18', spreads_bp: { '3M': null, '6M': null, '1Y': 140.5, '2Y': null, '3Y': 240, '5Y': 300, '7Y': null, '10Y': null }, recovery: 0.4, ccy: 'EUR' });
  assert.equal(MD.isDirty(d, CURVE), false);
  d.spreads['5Y'] = '300,0'; assert.equal(MD.isDirty(d, CURVE), false, '«300,0» — то же, что «300»');
  d.spreads['5Y'] = '305'; assert.equal(MD.isDirty(d, CURVE), true);
  d.spreads['5Y'] = '300'; d.recovery = ''; assert.equal(MD.isDirty(d, CURVE), true, 'пусто = 25 %, а сохранено 40 %');
  d.recovery = '40'; d.ccy = ''; assert.equal(MD.isDirty(d, CURVE), true, 'пусто = USD, а сохранено EUR');
  d.ccy = 'eur'; assert.equal(MD.isDirty(d, CURVE), false);
  d.spreads['5Y'] = 'abc'; assert.equal(MD.isDirty(d, CURVE), true);
  assert.equal(MD.readDraft('A', '2026-11-18', d).errors['5Y'].startsWith('5Y'), true);
  // без кривой: пустая строка не изменена, любой ввод — изменение
  const e = MD.draftOf(null);
  assert.equal(MD.isDirty(e, null), false);
  assert.equal(MD.readDraft('A', '2026-11-18', e).errors.spreads, 'введите хотя бы один срок');
  e.ccy = 'EUR'; assert.equal(MD.isDirty(e, null), true);
});

test('строки: кривые даты, CDS-позиции без кривой и добавленные имена — по имени, без повторов', () => {
  const list = { curves: [CURVE, Object.assign({}, CURVE, { name_key: 'эмитент б', name: 'Эмитент Б', valid_from: '2026-11-18' })],
    positions: [{ name: 'Эмитент А', name_key: 'эмитент а', positions: 2, has_curve: true }, { name: 'CDS Г', name_key: 'cds г', positions: 1, has_curve: false }] };
  const rows = MD.mergeRows(list, ['эмитент   в', 'ЭМИТЕНТ Б', 'Эмитент Д']);
  assert.deepEqual(rows.map(r => r.name), ['Эмитент А', 'Эмитент Б', 'эмитент в', 'Эмитент Д', 'CDS Г']);
  assert.deepEqual(rows.map(r => [!!r.curve, r.positions]), [[true, 2], [true, 0], [false, 0], [false, 0], [false, 1]]);
  assert.deepEqual(MD.mergeRows(null, []), []);
  // копирование даст кривые, введённые раньше выбранной даты; введённые на неё — нет
  assert.deepEqual(MD.copyEligible(rows, '2026-11-18'), ['Эмитент А']);
  assert.deepEqual(MD.copyEligible(rows, '2026-11-19'), ['Эмитент А', 'Эмитент Б']);
  assert.deepEqual(MD.copyEligible(rows, '2026-11-16'), []);
});

test('права: роль из /api/v1/auth/state; вход выключен — только компьютер сервера; без ответа — по адресу', () => {
  const U = role => ({ enabled: true, user: role ? { username: 'u', role } : null });
  assert.equal(MD.rights(U('trader'), 'desk.example').editable, true);
  assert.equal(MD.rights(U('admin'), 'desk.example').editable, true);
  const v = MD.rights(U('viewer'), '127.0.0.1');
  assert.deepEqual([v.editable, /просмотр/.test(v.reason)], [false, true]);
  assert.equal(MD.rights(U(null), 'x').editable, false);
  assert.equal(MD.rights({ enabled: false, user: null, loopback: true }, 'desk.example').editable, true);
  const n = MD.rights({ enabled: false, user: null, loopback: false }, '127.0.0.1');
  assert.deepEqual([n.editable, /127\.0\.0\.1/.test(n.reason)], [false, true]);
  assert.equal(MD.rights(null, 'localhost').editable, true);
  assert.equal(MD.rights(null, '127.0.0.5').editable, true);
  assert.equal(MD.rights(null, 'desk.example').editable, false);
  assert.ok(/сервер отказал/.test(MD.WHY.forbidden));
});

test('сервер: адреса, методы, JSON; ответ — { status, body }; обрыв связи — status 0', async () => {
  const calls = [];
  const fake = (status, body) => (url, o) => { calls.push([url, o]); return Promise.resolve({ status, json: () => Promise.resolve(body) }); };
  let api = MD.serverApi(fake(200, { ok: 1 }));
  assert.deepEqual(await api.list('2026-11-18'), { status: 200, body: { ok: 1 } });
  assert.equal(calls[0][0], '../api/v1/market-data/manual/cds/curves?date=2026-11-18');
  assert.deepEqual(calls[0][1], { cache: 'no-store' });
  await api.history('Эмитент А');
  assert.equal(calls[1][0], '../api/v1/market-data/manual/cds/history?name=%D0%AD%D0%BC%D0%B8%D1%82%D0%B5%D0%BD%D1%82%20%D0%90');
  await api.put({ name: 'A', date: '2026-11-18', spreads_bp: { '5Y': 1 } });
  assert.equal(calls[2][0], '../api/v1/market-data/manual/cds/curve');
  assert.equal(calls[2][1].method, 'PUT'); assert.equal(calls[2][1].headers['Content-Type'], 'application/json');
  assert.deepEqual(JSON.parse(calls[2][1].body), { name: 'A', date: '2026-11-18', spreads_bp: { '5Y': 1 } });
  await api.copy({ to_date: '2026-11-18', names: null });
  assert.equal(calls[3][0], '../api/v1/market-data/manual/cds/copy'); assert.equal(calls[3][1].method, 'POST');
  assert.equal((await MD.serverApi(fake(403, { code: 'forbidden' })).put({})).status, 403);
  assert.equal((await MD.serverApi((u, o) => Promise.reject(new Error('net'))).list('2026-11-18')).status, 0);
  const html = { status: 502, json: () => Promise.reject(new Error('not json')) };
  assert.deepEqual(await MD.serverApi(() => Promise.resolve(html)).list('2026-11-18'), { status: 502, body: null });
});

test('демо: вымышленные имена, кривая действует до следующей введённой, правила сервера, копирование, история', async () => {
  const api = MD.demoApi('2026-10-21'), D = '2026-10-21';
  assert.equal(api.demo, true);
  const l0 = (await api.list(D)).body;
  assert.deepEqual(l0.tenors, TENORS);
  assert.deepEqual(l0.curves.map(c => c.name), ['Эмитент А', 'Эмитент Б', 'Эмитент В']);
  assert.deepEqual(l0.curves.map(c => c.entered_on_date), [false, false, false]);
  assert.equal(l0.curves[1].valid_from, '2026-10-20', 'Б: правка на день раньше перекрывает ввод за три дня');
  assert.deepEqual(l0.positions.map(p => [p.name, p.has_curve]), [['CDS Эмитент Г 5Y', false], ['Эмитент А', true]]);
  assert.deepEqual((await api.list('2026-10-01')).body.curves, [], 'до первого ввода кривых нет');
  // ввод: меняет, повтор — нет; ошибки — 422 с текстом
  const put = (b) => api.put(Object.assign({ name: 'Эмитент Д', date: D, spreads_bp: { '5Y': 400 } }, b));
  let r = (await put({})).body;
  assert.deepEqual([r.changed, r.curve.entered_on_date, r.curve.recovery, r.curve.ccy, r.curve.changed_by], [true, true, 0.25, 'USD', 'demo']);
  assert.equal((await put({ name: ' Эмитент   Д ' })).body.changed, false);
  assert.equal((await put({ spreads_bp: { '5Y': 401 } })).body.changed, true);
  for (const bad of [{ spreads_bp: { '5Y': -1 } }, { spreads_bp: { '2.5Y': 1 } }, { spreads_bp: {} }, { recovery: 1.5 }, { ccy: 'EU' }, { name: '  ' }]) {
    const x = await put(bad); assert.equal(x.status, 422, JSON.stringify(bad)); assert.ok(x.body.message);
  }
  // ввод на прошлую дату не трогает позднюю
  await api.put({ name: 'Эмитент Д', date: '2026-10-19', spreads_bp: { '5Y': 380 } });
  const past = (await api.list('2026-10-19')).body.curves.find(c => c.name === 'Эмитент Д');
  assert.deepEqual([past.spreads_bp['5Y'], past.entered_on_date], [380, true]);
  assert.equal((await api.list(D)).body.curves.find(c => c.name === 'Эмитент Д').spreads_bp['5Y'], 401);
  // история: новые сверху, у каждой ревизии кто и когда
  const h = (await api.history('эмитент   д')).body;
  assert.deepEqual(h.revisions.map(v => [v.valid_from, v.spreads_bp['5Y']]), [['2026-10-19', 380], [D, 401], [D, 400]]);
  assert.ok(h.revisions.every(v => v.changed_by === 'demo' && v.changed_at));
  // снять с даты, и снова ничего не снимать
  assert.deepEqual((await api.put({ name: 'Эмитент Д', date: '2026-10-22', clear: true })).body, { changed: true, curve: null });
  assert.deepEqual((await api.put({ name: 'Эмитент Д', date: '2026-10-22', clear: true })).body, { changed: false, curve: null });
  assert.equal((await api.list('2026-10-22')).body.curves.some(c => c.name === 'Эмитент Д'), false);
  assert.equal((await api.history('Эмитент Д')).body.revisions[0].cleared, true);
  // копирование: что действовало до даты; своё на дату не трогаем; названное без кривой — в «пропущено»
  const c1 = (await api.copy({ to_date: '2026-10-23', names: ['эмитент б'] })).body;
  assert.deepEqual([c1.copied.map(x => x.name), c1.skipped], [['Эмитент Б'], []]);
  assert.equal(c1.copied[0].entered_on_date, true);
  assert.deepEqual(c1.copied[0].spreads_bp, (await api.list('2026-10-21')).body.curves[1].spreads_bp);
  const c2 = (await api.copy({ to_date: '2026-10-23' })).body;
  assert.deepEqual(c2.copied.map(x => x.name).sort(), ['Эмитент А', 'Эмитент В']);
  assert.deepEqual(c2.skipped.map(s => [s.name, s.why]), [['Эмитент Б', 'на эту дату кривая уже введена']]);
  const c3 = (await api.copy({ to_date: '2026-10-23', names: ['Эмитент Я'] })).body;
  assert.deepEqual([c3.copied, c3.skipped.map(s => s.why)], [[], ['нет кривой на дату, с которой копируем']]);
  assert.equal((await api.copy({ to_date: '2026-10-23', from_date: '2026-10-23' })).status, 422);
  const c4 = (await api.copy({ to_date: '2026-10-25', from_date: '2026-10-19', names: ['Эмитент Д'] })).body;
  assert.equal(c4.copied[0].spreads_bp['5Y'], 380, 'с заданной даты — то, что действовало на неё');
});

test('экран в каталоге, подписан в шапке «фильтр здесь не действует», без прогона, читает сервер и демо', () => {
  const cat = fs.readFileSync(path.join(ROOT, 'prototypes', 'concepts', 'catalog.js'), 'utf8');
  assert.ok(/\['c22-manual-md', 'C22', 'Ручные рыночные данные'/.test(cat));
  const html = fs.readFileSync(path.join(ROOT, 'prototypes', 'concepts', 'c22-manual-md.html'), 'utf8');
  assert.ok(/data-run="optional"/.test(html) && /K\.top\(\{[^)]*filter: false/.test(html));
  assert.ok(html.includes('lib/manual-md.js') && html.includes('MD.serverApi') && html.includes('MD.demoApi'));
  for (const t of ['Добавить имя', 'Скопировать с прошлой даты', 'введено вручную', 'История', 'Только чтение', 'Как заданы котировки', 'допущение'])
    assert.ok(html.includes(t), t);
});
