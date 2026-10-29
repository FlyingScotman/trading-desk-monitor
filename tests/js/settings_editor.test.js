// Редактор по JSON Schema (prototypes/settings-forms.js, SF.Editor; TDM#421): формы для любого файла настроек и любого
// ключа, без правки JSON руками. Вложенные группы, списки строк (добавить / убрать / поднять), таблицы (массивы объектов,
// со списками внутри строки), «ключ → значение» (источник → список шаблонов), перечисления с подписями (oneOf / const),
// «по умолчанию» у полей с null, только чтение, шаблон строки (ошибка у поля), default — подсказка в пустом поле,
// комментарии «_…» и ключи вне формы не трогаются, additionalProperties: false — без «добавить ключ».
// Браузер не нужен: заглушка DOM ниже (элементы, атрибуты, события, querySelector по #id, тегу и .классу).
//   node tests/js/settings_editor.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const ROOT = path.join(__dirname, '..', '..');

// ---- заглушка DOM ----
class Node_ {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.attrs = {}; this.listeners = {}; this.dataset = {};
    this.className = ''; this.value = ''; this.checked = false; this.disabled = false; this.style = {}; this.parent = null; this._text = '';
    this.classList = { add: c => { if (!this.className.split(' ').includes(c)) this.className = (this.className + ' ' + c).trim(); },
      remove: c => { this.className = this.className.split(' ').filter(x => x !== c).join(' '); }, contains: c => this.className.split(' ').includes(c),
      toggle: (c, on) => { if (on === undefined ? !this.classList.contains(c) : on) this.classList.add(c); else this.classList.remove(c); } };
    this.isConnected = true; this.nodeType = 1; }
  get id() { return this.attrs.id || ''; }
  set id(v) { this.attrs.id = v; }
  get options() { return this.children.filter(c => c.tagName === 'OPTION'); }
  get lastChild() { return this.children[this.children.length - 1]; }
  get textContent() { return this._text + this.children.map(c => c.nodeType === 3 ? c.text : c.textContent).join(''); }
  set textContent(v) { this.children.forEach(c => { c.parent = null; }); this.children = []; this._text = String(v); }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'selected') this.selected = true; }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  fire(t, extra) { (this.listeners[t] || []).forEach(f => f(Object.assign({ target: this, key: '', preventDefault() {}, stopPropagation() {} }, extra || {}))); }
  _adopt(c) { if (typeof c === 'string') c = { nodeType: 3, text: c }; c.parent = this; return c; }
  append(...cs) { cs.forEach(c => this.children.push(this._adopt(c))); }
  prepend(...cs) { this.children.unshift(...cs.map(c => this._adopt(c))); }
  insertBefore(c, ref) { const i = this.children.indexOf(ref); this.children.splice(i < 0 ? this.children.length : i, 0, this._adopt(c)); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); this.parent = null; }
  replaceWith(n) { const p = this.parent; if (!p) return; p.children[p.children.indexOf(this)] = p._adopt(n); this.parent = null; }
  focus() { doc.activeElement = this; }
  all() { return this.children.filter(c => c.nodeType === 1).flatMap(c => [c, ...c.all()]); }
  matches(sel) {
    return sel.split(',').some(s => { s = s.trim();
      if (s[0] === '#') return this.id === s.slice(1).replace(/\\/g, '');
      if (s[0] === '.') return this.classList.contains(s.slice(1));
      const m = /^(\w+)(?::not\(\[type=checkbox\]\))?$/.exec(s); return !!m && this.tagName === m[1].toUpperCase() && !(s.includes(':not') && this.attrs.type === 'checkbox'); });
  }
  querySelector(sel) { return this.all().find(e => e.matches(sel)) || null; }
  querySelectorAll(sel) { return this.all().filter(e => e.matches(sel)); }
}
// select: value — по выбранному option
function makeSelect(n) {
  let v = null;
  Object.defineProperty(n, 'value', { get() { const o = n.options; if (v !== null && o.some(x => x.value === v)) return v; return o.length ? o[0].value : ''; },
    set(x) { v = String(x); } });
}
const doc = { activeElement: null, body: new Node_('body'),
  createElement(tag) { const n = new Node_(tag); if (tag === 'select') makeSelect(n); return n; },
  addEventListener() {}, removeEventListener() {}, getElementById(id) { return doc.body.querySelector('#' + id); } };
const ctx = { console, document: doc, prompt: () => null, alert: msg => { ctx.alerted = msg; }, confirm: () => true };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(ROOT, 'prototypes', 'settings-forms.js'), 'utf8'), ctx);
const SF = ctx.SETTINGS_FORMS;
const plain = v => v === undefined ? v : JSON.parse(JSON.stringify(v));
const eq = (a, b, m) => assert.deepStrictEqual(plain(a), plain(b), m);
let n = 0;
const t = (name, f) => { f(); n++; console.log('ok', name); };
const type = (el, v) => { el.value = v; el.fire('input'); el.fire('change'); };
const btn = (root, text) => root.all().find(e => e.tagName === 'BUTTON' && e.textContent.trim().startsWith(text));
const inputs = root => root.all().filter(e => e.tagName === 'INPUT');

// синтетическая схема «как у адаптера»: всё, что может встретиться в его файлах настроек
const SCHEMA = {
  type: 'object', additionalProperties: false, patternProperties: { '^_': {} },
  properties: {
    folders: { type: 'array', title: 'Папки', items: { type: 'string', minLength: 1 }, description: 'по одной в строке' },
    sources: { type: 'object', title: 'Шаблоны имён', description: 'источник → список шаблонов', propertyNames: { pattern: '^[a-z_]+$' },
               additionalProperties: { type: 'array', items: { type: 'string', minLength: 1 } } },
    mode: { title: 'Режим', oneOf: [{ const: 'fast', title: 'Быстро' }, { const: 'full', title: 'Полностью' }] },
    level: { type: ['integer', 'null'], title: 'Уровень', minimum: 0, default: 3 },
    strict: { type: ['boolean', 'null'], title: 'Строго' },
    code: { type: 'string', title: 'Код', pattern: '^[A-Z]{3}$' },
    version: { type: 'string', title: 'Версия', readOnly: true },
    parser: { type: 'object', title: 'Разбор', properties: {
      sep: { type: 'string', title: 'Разделитель', enum: [';', ','] },
      limits: { type: 'object', title: 'Пределы', properties: { rows: { type: 'integer', title: 'Строк', default: 1000 } }, additionalProperties: false } } },
    books: { type: 'array', title: 'Книги', items: { type: 'object', required: ['id'], additionalProperties: false, properties: {
      id: { type: 'string', title: 'Код', minLength: 1 }, active: { type: 'boolean', title: 'Действует', default: true },
      codes: { type: 'object', title: 'Коды', additionalProperties: { type: 'array', items: { type: 'string', minLength: 1 } } } } } }
  }
};

function editor(data) {
  const E = new SF.Editor({ schema: SCHEMA, data });
  E.validate();
  const el = E.renderFields(SF.columns(SCHEMA), E.data, () => { E.validate(); E.refresh(); }, []);
  return { E, el };
}

t('виды полей по схеме: списки, «ключ → значение», перечисления oneOf, null, только чтение, таблицы', () => {
  const k = {}; SF.columns(SCHEMA).forEach(c => { k[c.key] = c; });
  eq(Object.fromEntries(Object.entries(k).map(([a, c]) => [a, c.kind])), { folders: 'strlist', sources: 'codes', mode: 'enum', level: 'integer',
    strict: 'bool', code: 'text', version: 'text', parser: 'object', books: 'table' });
  eq(k.mode.options.map(o => o.label), ['Быстро', 'Полностью']);
  assert.ok(k.level.nullable && k.strict.nullable && k.version.readOnly);
  assert.strictEqual(SF.placeholder(k.level), 'по умолчанию: 3');
  assert.strictEqual(SF.kind({ type: 'object', additionalProperties: { type: 'integer' } }), 'map');
  assert.strictEqual(SF.kind({ type: 'object' }), 'map', 'объект без полей: «ключ → строка», не JSON');
  assert.strictEqual(SF.kind({ type: 'object', properties: {}, additionalProperties: false }), 'object');
  eq(SF.mapKeys({ a: 1, _note: 'x', id: 2 }, { properties: { id: {} } }), ['a']);
  eq(Object.keys(SF.renameKey({ a: 1, b: 2, c: 3 }, 'b', 'z')), ['a', 'z', 'c']);
  eq(SF.emptyValue({ type: 'array' }), []); assert.strictEqual(SF.emptyValue({ type: 'string' }), '');
});

t('проверка: oneOf из const и const — как enum (то же на сервере: desk.settings_forms)', () => {
  const codes = d => SF.validate(SCHEMA, d).map(i => i.code + '|' + i.path.join('.')).sort();
  eq(codes({ mode: 'fast' }), []);
  eq(codes({ mode: 'slow' }), ['bad_value|mode']);
  eq(codes({ level: null, strict: null }), [], 'null у поля с null — «по умолчанию»');
  eq(codes({ code: 'usd' }), ['bad_value|code']);
  eq(codes({ x: 1, _comment: 'kept' }), ['unknown_config_key|x']);
  eq(SF.validate({ type: 'object', properties: { v: { const: 2 } } }, { v: 3 }).map(i => i.code), ['bad_value']);
});

t('список строк: добавить, ввести, поднять, убрать; пустой список уходит из файла', () => {
  const data = { _comment: 'не трогать', folders: ['C:/a'] }, { el } = editor(data);
  const list = el.querySelector('.sf-list');
  btn(list, '+ Добавить').fire('click');
  eq(data.folders, ['C:/a', '']);
  type(inputs(list)[1], 'C:/b');
  eq(data.folders, ['C:/a', 'C:/b']);
  btn(list, '↑').fire('click');
  eq(data.folders, ['C:/b', 'C:/a']);
  list.all().filter(e => e.attrs.title === 'Убрать').forEach(() => list.all().find(e => e.attrs.title === 'Убрать').fire('click'));
  assert.ok(!('folders' in data) && data._comment === 'не трогать');
});

t('«ключ → значение»: добавить ключ, переименовать (порядок ключей тот же), значение — список, убрать', () => {
  const data = { sources: { daily: ['^a_.*'] } }, { el } = editor(data);
  const map = el.querySelector('.sf-map');
  const add = map.all().find(e => e.tagName === 'INPUT' && e.attrs.placeholder === 'новый ключ');
  add.value = 'hourly'; btn(map.querySelector('.sf-map-add'), '+ Добавить').fire('click');
  eq(data.sources, { daily: ['^a_.*'], hourly: [] });
  const keys = () => map.all().filter(e => e.classList.contains('sf-key') && e.attrs['aria-label'] === 'ключ');
  const k0 = keys()[0]; k0.value = 'daily_v2'; k0.fire('change');
  eq(Object.keys(data.sources), ['daily_v2', 'hourly']);
  const k1 = keys()[1]; k1.value = 'daily_v2'; k1.fire('change');
  assert.ok(/уже есть/.test(ctx.alerted)); eq(Object.keys(data.sources), ['daily_v2', 'hourly']);
  const row = map.all().filter(e => e.classList.contains('sf-map-row'))[1];
  btn(row, '+ Добавить').fire('click'); type(inputs(row.querySelector('.sf-list'))[0], '^b_');
  eq(data.sources.hourly, ['^b_']);
  map.all().filter(e => e.attrs['aria-label'] === 'Убрать ключ')[0].fire('click');
  eq(data.sources, { hourly: ['^b_'] });
});

t('перечисление с подписями, «по умолчанию» у null, только чтение, ошибка шаблона под полем', () => {
  const data = { level: null, version: '1.0', code: 'RUB' }, { E, el } = editor(data);
  const sel = el.querySelector('#' + E.ctlId(['mode']));
  eq(sel.options.map(o => o.textContent), ['—', 'Быстро', 'Полностью']);
  sel.value = 'full'; sel.fire('change');
  assert.strictEqual(data.mode, 'full');
  const strict = el.querySelector('#' + E.ctlId(['strict']));
  eq(strict.options.map(o => o.textContent), ['по умолчанию', 'да', 'нет']);
  strict.value = 'false'; strict.fire('change'); assert.strictEqual(data.strict, false);
  strict.value = ''; strict.fire('change'); assert.ok(!('strict' in data));
  const lvl = el.querySelector('#' + E.ctlId(['level']));
  assert.strictEqual(lvl.attrs.placeholder, 'по умолчанию: 3');
  assert.ok('readonly' in el.querySelector('#' + E.ctlId(['version'])).attrs);
  const code = el.querySelector('#' + E.ctlId(['code']));
  type(code, 'usd');
  const cell = code.parent;
  assert.ok(cell.classList.contains('bad') && /недопустимое значение/.test(cell.textContent), cell.textContent);
  type(code, 'USD');
  assert.ok(!cell.classList.contains('bad'));
});

t('вложенные группы: сворачиваются, пустая уходит из файла, ключи вне формы и комментарии остаются', () => {
  const data = { parser: { _note: 'x', sep: ';' } }, { E, el } = editor(data);
  const grp = el.querySelector('.sf-grp');
  assert.strictEqual(grp.tagName, 'DETAILS');
  const inner = grp.querySelectorAll('.sf-grp');
  assert.strictEqual(inner.length, 1, 'группа в группе');
  type(el.querySelector('#' + E.ctlId(['parser', 'limits', 'rows'])), '50');
  eq(data.parser, { _note: 'x', sep: ';', limits: { rows: 50 } });
  type(el.querySelector('#' + E.ctlId(['parser', 'limits', 'rows'])), '');
  eq(data.parser, { _note: 'x', sep: ';' }, 'пустая группа «Пределы» убрана');
});

t('таблица: простые поля в ячейках, список кодов строки — «поля…», копия и удаление строки', () => {
  const data = { books: [{ id: 'B1' }] }, { E, el } = editor(data);
  const tbl = el.querySelector('.sf-subtable');
  btn(tbl, '+ Добавить строку').fire('click');
  eq(data.books, [{ id: 'B1' }, {}]);
  type(tbl.querySelector('#' + E.ctlId(['books', 1, 'id'])), 'B2');
  btn(tbl, 'поля…').fire('click');
  const codes = tbl.querySelector('.sf-map');
  const add = codes.all().find(e => e.attrs.placeholder === 'новый ключ');
  add.value = '*'; btn(codes.querySelector('.sf-map-add'), '+ Добавить').fire('click');
  btn(codes.querySelector('.sf-list'), '+ Добавить').fire('click');
  type(inputs(codes.querySelector('.sf-list'))[0], 'X1');
  eq(data.books[0], { id: 'B1', codes: { '*': ['X1'] } });
  tbl.all().filter(e => e.attrs.title === 'Удалить строку')[1].fire('click');
  eq(data.books.map(b => b.id), ['B1']);
  E.validate();
  eq(E.errors(), []);
});

t('additionalProperties: false — ключа не добавить; корень «ключ → значение» (вкладка из одних ключей)', () => {
  const closed = new SF.Editor({ schema: { type: 'object', properties: { a: { type: 'object', title: 'A', properties: { x: { type: 'string' } }, additionalProperties: false } } }, data: {} });
  const el = closed.renderFields(SF.columns(closed.schema), closed.data, () => {}, []);
  assert.ok(!el.querySelector('.sf-map'), 'нет редактора лишних ключей');
  const rootMap = { type: 'object', additionalProperties: { type: 'array', items: { type: 'string' } } };
  const R = new SF.Editor({ schema: rootMap, data: { _c: 1, s1: ['^x'] } });
  const box = R.renderExtras(rootMap, R.data, () => {}, [], 'Ключи файла');
  eq(box.all().filter(e => e.attrs['aria-label'] === 'ключ').map(e => e.value), ['s1'], 'комментарий «_c» не показан и не тронут');
});

console.log(n + ' tests ok');
