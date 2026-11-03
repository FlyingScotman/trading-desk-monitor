// Навигация настроек и длинные таблицы — чистая часть (TDM#551, TDM#552): адрес подраздела, выбор подраздела,
// поиск по настройкам; свёртка, поиск и CSV длинной таблицы.
//   node --test tests/js/settings_nav.test.js
'use strict';
const test = require('node:test');
const assert = require('assert');
const path = require('path');

require(path.join(__dirname, '..', '..', 'prototypes', 'settings-nav.js'));
require(path.join(__dirname, '..', '..', 'prototypes', 'settings-longtable.js'));
const N = globalThis.DeskSettingsNav._test, L = globalThis.DeskLongTable._test;

test('адрес подраздела: #раздел/подраздел', () => {
  assert.deepStrictEqual(N.split('#adapter/folders'), { id: 'adapter', sub: 'folders' });
  assert.deepStrictEqual(N.split('adapter'), { id: 'adapter', sub: null });
  assert.deepStrictEqual(N.split('#dirs'), { id: 'dirs', sub: null });
  assert.deepStrictEqual(N.split('#tab-sources'), { id: 'tab-sources', sub: null });
  assert.deepStrictEqual(N.split(''), { id: '', sub: null });
  assert.deepStrictEqual(N.split('#a/b/c'), { id: 'a/b/c', sub: null }, 'не подраздел');
});

test('какой подраздел открыть: из адреса, иначе запомненный, иначе первый', () => {
  const av = ['overview', 'folders', 'check'];
  assert.strictEqual(N.pickSub('check', 'folders', av), 'check');
  assert.strictEqual(N.pickSub(null, 'folders', av), 'folders');
  assert.strictEqual(N.pickSub('services', 'gone', av), 'overview', 'скрытый (место без модуля) и неизвестный — первый');
  assert.strictEqual(N.pickSub(null, null, []), null);
});

test('поиск по настройкам: все слова, подпись важнее пути, ё = е, ключ settings.json', () => {
  const items = [
    { kind: 'field', label: 'Ждать ответа хаба', path: 'Подключения › Маркет-дата сервер', extra: 'hub_timeout_s На один запрос', target: 'hub_timeout_s' },
    { kind: 'field', label: 'Архив принятых файлов', path: 'Адаптер и источники › Папки › Папки с отчётами', extra: 'inbox_archive', target: 'inbox_archive' },
    { kind: 'card', label: 'Папки с отчётами', path: 'Адаптер и источники › Папки', extra: '', target: 'dirs-card' },
    { kind: 'sec', label: 'Подключения', path: 'раздел', extra: 'хаб', target: 'hub' },
    { kind: 'field', label: 'Отчёты не старше', path: 'Автообновление › Цикл', extra: 'inbox_lookback_days', target: 'inbox_lookback_days' },
  ];
  assert.deepStrictEqual(N.search(items, 'папки').map(x => x.target), ['dirs-card', 'inbox_archive'], 'по подписи — выше, чем по пути');
  assert.deepStrictEqual(N.search(items, 'inbox_archive').map(x => x.target), ['inbox_archive']);
  assert.deepStrictEqual(N.search(items, 'отчеты старше').map(x => x.target), ['inbox_lookback_days'], 'ё = е, оба слова');
  assert.deepStrictEqual(N.search(items, 'хаба ждать').map(x => x.target), ['hub_timeout_s']);
  assert.deepStrictEqual(N.search(items, '   '), []);
  assert.deepStrictEqual(N.search(items, 'нет такого'), []);
  assert.strictEqual(N.search(items, 'а', 2).length, 2, 'не больше предела');
  assert.strictEqual(N.norm('«Ёлка»  Б'), 'елка б');
});

test('длинная таблица: свёрнута после предела, поиск по ячейкам, CSV для Excel', () => {
  assert.deepStrictEqual(L.fold(10, false), { folded: false, show: 10 });
  assert.deepStrictEqual(L.fold(L.FOLD, false), { folded: false, show: L.FOLD });
  assert.deepStrictEqual(L.fold(180, false), { folded: true, show: L.HEAD });
  assert.deepStrictEqual(L.fold(180, true), { folded: true, show: 180 });
  const rows = [['Тип 01', 'подтип 01', 5], ['Тип 01', 'подтип 02', 7], ['Тип 02', 'подтип 01', null]];
  assert.deepStrictEqual(L.filter(rows, 'тип 01'), [rows[0], rows[1]], 'запрос целиком с начала слова: не «подтип 01»');
  assert.deepStrictEqual(L.filter(rows, '02 подтип'), [rows[1], rows[2]], 'слово в любой ячейке');
  assert.deepStrictEqual(L.filter(rows, 'подтип 02'), [rows[1]]);
  assert.deepStrictEqual(L.filter(rows, 'ТИП 02 подтип 01'), [rows[1], rows[2]], 'через ячейки — по словам');
  assert.deepStrictEqual(L.filter(rows, '07'), []);
  assert.deepStrictEqual(L.filter(rows, ''), rows);
  assert.notStrictEqual(L.filter(rows, ''), rows, 'копия');
  assert.strictEqual(L.csv(['a', 'b'], [['x;y', 'он сказал "да"'], [null, 3]]), '﻿a;b\r\n"x;y";"он сказал ""да"""\r\n;3\r\n');
  assert.strictEqual(L.fileName('Типы строк отчёта'), 'типы_строк_отчета.csv');
  assert.strictEqual(L.fileName(''), 'таблица.csv');
  assert.strictEqual(L.plural(1, 'строка', 'строки', 'строк'), '1 строка');
  assert.strictEqual(L.plural(22, 'строка', 'строки', 'строк'), '22 строки');
  assert.strictEqual(L.plural(11, 'строка', 'строки', 'строк'), '11 строк');
});
