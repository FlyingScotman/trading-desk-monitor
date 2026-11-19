// Формат чисел колонки (TDM#789) на экранах со своими колонками (Playwright): C09 «наши против рынка», C10 «необъяснённый остаток»,
// C13 список эмитентов и «Наши бумаги эмитента», C17 «Прогнозное кэрри». На каждом экране: по умолчанию вид прежний, знаки и единицы
// из меню «Колонки ▾» меняют ячейки, итог, подпись единицы и CSV, сброс колонки возвращает прежний вид; колонка каталога
// (MV, G-спред, PnL) — один формат с таблицами позиций других экранов; у списка эмитентов C13 кнопки «Колонки ▾» нет, формат
// на нём действует (задан в таблице бумаг той же страницы).
//   node --test tests/js/col_format_789_screens.pw.test.js
// Нужен Playwright, который находит require('playwright') (NODE_PATH, как в CI); без него тест пропускается.
'use strict';
const test = require('node:test');
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const { pathToFileURL } = require('url');
const { openWith } = require('./fixtures/pw_pages');

const pw = (() => { try { return require('playwright'); } catch (e) { return null; } })();
const PAGE = f => pathToFileURL(path.join(__dirname, '..', '..', 'prototypes', 'concepts', f)).href;
const skip = !pw && 'нет Playwright';
const KEY = 'tdm.view.v1:colfmt';

async function launch(width) {
  const browser = await pw.chromium.launch();
  const ctx = await browser.newContext({ viewport: { width, height: 900 }, acceptDownloads: true });
  const pg = await ctx.newPage(), errors = [];
  pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
  pg.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
  return { browser, pg, errors };
}
// ready уходит в страницу как текст функции: замыканий у неё нет, селектор вписывается в тело
const readyTbl = sel => new Function('return !!(window.KIT && document.querySelector(' + JSON.stringify(sel) + '))');
const openMenu = async pg => { await pg.click('.k-colbtn'); await pg.waitForSelector('.k-colmenu'); };
const fmtBtn = (pg, key) => pg.click('.k-colmenu .k-colmenu-fmtbtn[data-fmt="' + key + '"]');
const selD = (pg, key) => pg.locator('.k-colmenu select[data-fmt-d="' + key + '"]');
const selU = (pg, key) => pg.locator('.k-colmenu select[data-fmt-u="' + key + '"]');
const resetFmt = (pg, key) => pg.click('.k-colmenu button[data-fmt-reset="' + key + '"]');
const csvOf = async (pg, scope) => { const [d] = await Promise.all([pg.waitForEvent('download'), pg.click((scope || '') + ' .k-tbl-ctl .k-tbtn:has-text("CSV")')]); return fs.readFileSync(await d.path(), 'utf8').replace(/^﻿/, ''); };
// колонка таблицы, в которой есть th[data-k=key] (при нескольких — первая из scope): подпись, первая строка и итог
const colOf = (pg, key, scope) => pg.evaluate(([k, sc]) => {
  const t = [...document.querySelectorAll((sc || '') + ' table.k-tbl')].find(x => x.querySelector('th[data-k="' + k + '"]')), th = t.querySelector('th[data-k="' + k + '"]'), i = th.cellIndex;
  const r = t.querySelector('tbody tr.row'), foot = t.querySelector('tfoot tr');
  let at = 0, total = null;
  if (foot) for (const c of foot.cells) { if (i >= at && i < at + c.colSpan) { total = c.textContent.trim(); break; } at += c.colSpan; }
  return { head: th.textContent.replace(/\s+/g, ' ').trim(), first: r.cells[i].textContent.trim(), total };
}, [key, scope]);
const NUM = /^[+(−-]?[\d,]+(\.\d+)?\)?$/;
const dec = s => (/\.(\d+)/.exec(s) || [, ''])[1].length;

test('C09 «наши против рынка»: «рынок», «разница» — свой формат, G-спред и MV — общие с таблицами позиций; по умолчанию вид прежний', { skip }, async () => {
  const { browser, pg, errors } = await launch(1500);
  try {
    await openWith(pg, PAGE('c09-market.html'), readyTbl('#rv table.k-tbl tbody tr.row'));
    await pg.waitForSelector('.k-colbtn');
    const was = {}; for (const k of ['gspread', 'mkt', 'rv', 'mv']) was[k] = await colOf(pg, k, '#rv');
    assert.ok(/^\d[\d,]*$/.test(was.gspread.first) && /^\d[\d,]*$/.test(was.mkt.first), 'G-спред и рынок — целые б.п.: ' + was.gspread.first + ' / ' + was.mkt.first);
    assert.ok(/^\+\d+$|^\(\d+\)$/.test(was.rv.first), '«разница» — целая, со знаком «+»: ' + was.rv.first);
    assert.ok(was.mv.head === 'MV, mm ₽' && !/\./.test(was.mv.first), 'MV — mm ₽ без знаков после точки: ' + was.mv.head + ' ' + was.mv.first);
    await openMenu(pg);
    const withBtn = await pg.evaluate(() => [...document.querySelectorAll('.k-colmenu-row')].filter(r => r.querySelector('.k-colmenu-fmtbtn')).map(r => r.querySelector('input').dataset.col));
    for (const k of ['ytm', 'gspread', 'mkt', 'rv', 'mv']) assert.ok(withBtn.includes(k), 'кнопка формата у ' + k);
    for (const k of ['name', 'ccy', 'rating', 'tenor']) assert.ok(!withBtn.includes(k), 'у ' + k + ' кнопки нет');
    // собственные колонки: «рынок» 2 знака, «разница» 1 знак; у них только знаки — единиц нет
    await fmtBtn(pg, 'mkt'); await selD(pg, 'mkt').selectOption('2');
    await fmtBtn(pg, 'rv'); await selD(pg, 'rv').selectOption('1');
    assert.strictEqual(await selU(pg, 'mkt').count(), 0);
    const mkt = await colOf(pg, 'mkt', '#rv'), rv = await colOf(pg, 'rv', '#rv');
    assert.ok(NUM.test(mkt.first) && dec(mkt.first) === 2 && Math.round(parseFloat(mkt.first.replace(/,/g, ''))) === parseInt(was.mkt.first.replace(/,/g, '')), 'рынок: ' + mkt.first);
    assert.ok(/^\+\d+\.\d$/.test(rv.first) || /^\(\d+\.\d\)$/.test(rv.first), 'разница: ' + rv.first);
    // колонка каталога: G-спред — 1 знак; MV — bn с подписью единицы
    await fmtBtn(pg, 'gspread'); await selD(pg, 'gspread').selectOption('1');
    await fmtBtn(pg, 'mv'); await selU(pg, 'mv').selectOption('bn');
    assert.strictEqual(dec((await colOf(pg, 'gspread', '#rv')).first), 1);
    const mv = await colOf(pg, 'mv', '#rv');
    assert.ok(mv.head.startsWith('MV, bn ₽') && dec(mv.first) >= 3, 'MV в bn: ' + mv.head + ' ' + mv.first);
    // CSV — те же числа и подпись единицы
    await pg.keyboard.press('Escape');
    const csv = (await csvOf(pg, '#rv')).split(/\r?\n/);
    assert.ok(/MV, bn ₽/.test(csv.join('\n')), 'в CSV подпись единицы MV: ' + csv.slice(0, 4).join(' | '));
    assert.ok(csv.some(l => l.includes(',' + mkt.first.replace(/,/g, '') + ',')), 'в CSV «рынок» с 2 знаками: ' + mkt.first);
    // сброс колонок — вид прежний
    await openMenu(pg);
    for (const k of ['mkt', 'rv', 'gspread', 'mv']) { await fmtBtn(pg, k); await resetFmt(pg, k); }
    for (const k of ['gspread', 'mkt', 'rv', 'mv']) assert.deepStrictEqual(await colOf(pg, k, '#rv'), was[k], 'после сброса ' + k);
    assert.deepStrictEqual(errors, []);
  } finally { await browser.close(); }
});

test('C10 «необъяснённый остаток»: PnL — формат «PnL дня» (день) и «PnL MTD» (MTD), «+» и тысячи как были; «Доля» — своя колонка, %', { skip }, async () => {
  const { browser, pg, errors } = await launch(1500);
  try {
    await openWith(pg, PAGE('c10-pnl-explain.html'), readyTbl('table.k-tbl th[data-k="ptot"]'), { 'c10-mode': 'day' });
    await pg.waitForSelector('.k-colbtn');
    const was = {}; for (const k of ['ptot', 'pexp', 'poth', 'share']) was[k] = await colOf(pg, k);
    assert.ok(was.ptot.head.endsWith('тыс ₽') && /^\+?\(?[\d,]+\)?$/.test(was.ptot.first), 'день: тыс ₽, целые: ' + was.ptot.head + ' ' + was.ptot.first);
    assert.ok(/^\d+%$/.test(was.share.first), 'доля — целые проценты: ' + was.share.first);
    const note = await pg.textContent('.unx');
    assert.strictEqual(note, 'тыс ₽');
    // PnL итог — 2 знака, единицы mm: все три колонки PnL одного формата; «+» у положительных остаётся
    await openMenu(pg);
    await fmtBtn(pg, 'ptot'); await selU(pg, 'ptot').selectOption('mm');
    const t = await colOf(pg, 'ptot'), o = await colOf(pg, 'poth');
    assert.ok(t.head.endsWith('mm ₽') && o.head.endsWith('mm ₽'), 'единица в подписи всех трёх колонок: ' + t.head);
    assert.ok(/^[+(]?[\d,]+\.\d+\)?$/.test(t.first) && /\d/.test(t.total), 'в mm со знаками после точки: ' + t.first + ' / итог ' + t.total);
    assert.strictEqual(await pg.textContent('.unx'), 'mm ₽', 'подпись панели следует за единицей');
    // «Доля» — 1 знак
    await fmtBtn(pg, 'share'); await selD(pg, 'share').selectOption('1');
    assert.ok(/^\d+\.\d%$/.test((await colOf(pg, 'share')).first), (await colOf(pg, 'share')).first);
    assert.strictEqual(await selU(pg, 'share').count(), 0, 'у процента только знаки');
    // CSV: подпись единицы и числа как на экране
    await pg.keyboard.press('Escape');
    const csv = await csvOf(pg, '.tb');
    assert.ok(/"?PnL итог, mm ₽"?/.test(csv), csv.split(/\r?\n/).slice(0, 4).join(' | '));
    // тот же формат у PnL в таблицах позиций: «PnL дня» на C01 — в mm
    assert.deepStrictEqual(JSON.parse(await pg.evaluate(k => localStorage.getItem(k), KEY)), { pnl: { decimals: null, scale: 'mm' }, unexplShare: { decimals: 1, scale: null } });
    // сброс — вид прежний
    await openMenu(pg);
    await fmtBtn(pg, 'ptot'); await resetFmt(pg, 'ptot'); await fmtBtn(pg, 'share'); await resetFmt(pg, 'share');
    for (const k of ['ptot', 'pexp', 'poth', 'share']) assert.deepStrictEqual(await colOf(pg, k), was[k], 'после сброса ' + k);
    // MTD: ключ «PnL MTD», по умолчанию mm ₽ с одним знаком; формат дня на него не действует
    await pg.keyboard.press('Escape');
    await pg.evaluate(() => localStorage.setItem('tdm.view.v1:colfmt', JSON.stringify({ pnl: { decimals: 4, scale: null } })));
    await openWith(pg, PAGE('c10-pnl-explain.html'), readyTbl('table.k-tbl th[data-k="ptot"]'), { 'c10-mode': 'mtd', [KEY]: JSON.stringify({ pnl: { decimals: 4, scale: null } }) });
    const m = await colOf(pg, 'ptot');
    assert.ok(m.head.endsWith('mm ₽') && /^[+(]?[\d,]+\.\d\)?$/.test(m.first), 'MTD: mm ₽, 1 знак, формат дня не действует: ' + m.head + ' ' + m.first);
    assert.deepStrictEqual(errors, []);
  } finally { await browser.close(); }
});

test('C13: список эмитентов (кнопки «Колонки ▾» у него нет) берёт формат MV, CS01, DV01, PnL; таблица бумаг эмитента на той же странице его меняет', { skip }, async () => {
  const { browser, pg, errors } = await launch(1500);
  try {
    const READY = readyTbl('#list table.k-tbl tbody tr.row');
    await openWith(pg, PAGE('c13-issuers.html'), READY);
    await pg.waitForSelector('.k-colbtn');
    assert.strictEqual(await pg.locator('#list .k-colbtn').count(), 0, 'у списка эмитентов кнопки «Колонки ▾» нет');
    const was = {}; for (const k of ['mv', 'cs01', 'dv01', 'pnl']) was[k] = await colOf(pg, k, '#list');
    assert.ok(was.mv.head.startsWith('MV, mm ₽') && !/\./.test(was.mv.first), was.mv.head + ' ' + was.mv.first);
    assert.ok(was.dv01.head.includes('тыс ₽/бп') && was.pnl.head.includes('тыс ₽'), was.dv01.head + ' / ' + was.pnl.head);
    // таблица бумаг эмитента — на K.cols, кнопка есть: MV в bn с 3 знаками, DV01 как есть — список меняется вместе с ней
    await openMenu(pg);
    await fmtBtn(pg, 'mv'); await selU(pg, 'mv').selectOption('bn'); await selD(pg, 'mv').selectOption('3');
    await fmtBtn(pg, 'dv01'); await selU(pg, 'dv01').selectOption('1');
    await fmtBtn(pg, 'pnl'); await selD(pg, 'pnl').selectOption('1');
    const mv = await colOf(pg, 'mv', '#list'), dv = await colOf(pg, 'dv01', '#list'), pnl = await colOf(pg, 'pnl', '#list');
    assert.ok(mv.head.startsWith('MV, bn ₽') && dec(mv.first) === 3 && dec(mv.total) === 3, 'MV в списке: ' + mv.head + ' ' + mv.first + ' / ' + mv.total);
    assert.ok(dv.head.includes('₽/бп') && !dv.head.includes('тыс'), dv.head);
    assert.strictEqual(dec(pnl.first), 1, 'PnL: 1 знак: ' + pnl.first);
    const bonds = await colOf(pg, 'mv', '.c13-bonds, #dash');
    assert.ok(bonds.head.startsWith('MV, bn ₽'), 'та же колонка в таблице бумаг: ' + bonds.head);
    // раскрытие ячейки по позициям — в тех же единицах, что в списке
    await pg.keyboard.press('Escape');
    const csv = await csvOf(pg);
    assert.ok(/MV, bn ₽/.test(csv), 'CSV списка: ' + csv.split(/\r?\n/).slice(0, 4).join(' | '));
    // перезагрузка — формат на месте (запись в браузере); сброс — вид прежний
    const saved = await pg.evaluate(k => localStorage.getItem(k), KEY);
    await openWith(pg, PAGE('c13-issuers.html'), READY, { [KEY]: saved });
    assert.deepStrictEqual(await colOf(pg, 'mv', '#list'), mv);
    await pg.evaluate(k => localStorage.removeItem(k), KEY);
    await openWith(pg, PAGE('c13-issuers.html'), READY);
    for (const k of ['mv', 'cs01', 'dv01', 'pnl']) assert.deepStrictEqual(await colOf(pg, k, '#list'), was[k], 'без формата ' + k + ' — как было');
    assert.deepStrictEqual(errors, []);
  } finally { await browser.close(); }
});

test('C17 «Прогнозное кэрри»: кэрри, фондирование, нетто и «Кэрри/DV01» — свой формат; итог, CSV и название CSV следуют за единицей', { skip }, async () => {
  const { browser, pg, errors } = await launch(1500);
  try {
    await openWith(pg, PAGE('c17-forward-carry.html'), readyTbl('table.k-tbl th[data-k="net"]'));
    await pg.waitForSelector('.k-colbtn');
    const was = {}; for (const k of ['carry', 'funding', 'net', 'cdv']) was[k] = await colOf(pg, k);
    assert.ok(was.carry.head.endsWith('mm ₽') && dec(was.carry.first) === 2 && dec(was.carry.total) === 2, 'по умолчанию mm ₽, 2 знака: ' + was.carry.head + ' ' + was.carry.first + ' / ' + was.carry.total);
    assert.ok(!/\./.test(was.cdv.first), '«Кэрри/DV01» — целые: ' + was.cdv.first);
    await openMenu(pg);
    const withBtn = await pg.evaluate(() => [...document.querySelectorAll('.k-colmenu-row')].filter(r => r.querySelector('.k-colmenu-fmtbtn')).map(r => r.querySelector('input').dataset.col));
    assert.deepStrictEqual(withBtn.sort(), ['carry', 'cdv', 'funding', 'net']);
    // кэрри — тыс ₽ (знаков по умолчанию на три меньше), нетто — 4 знака, у каждой колонки свой формат
    await fmtBtn(pg, 'carry'); await selU(pg, 'carry').selectOption('k');
    await fmtBtn(pg, 'net'); await selD(pg, 'net').selectOption('4');
    await fmtBtn(pg, 'cdv'); await selD(pg, 'cdv').selectOption('1');
    const c = await colOf(pg, 'carry'), n = await colOf(pg, 'net'), f = await colOf(pg, 'funding'), d = await colOf(pg, 'cdv');
    assert.ok(c.head.endsWith('тыс ₽') && dec(c.first) === 0 && /\d/.test(c.total), 'кэрри в тыс ₽: ' + c.head + ' ' + c.first + ' / ' + c.total);
    assert.ok(dec(n.first) === 4 && n.head.endsWith('mm ₽'), 'нетто, 4 знака: ' + n.first);
    assert.deepStrictEqual(f, was.funding, 'фондирование не тронуто');
    assert.strictEqual(dec(d.first), 1);
    assert.strictEqual(await selU(pg, 'cdv').count(), 0, 'у «Кэрри/DV01» только знаки');
    // CSV: подпись и название с единицами нетто, числа как на экране
    await pg.keyboard.press('Escape');
    const csv = await csvOf(pg);
    assert.ok(/Кэрри, тыс ₽/.test(csv), csv.split(/\r?\n/).slice(0, 4).join(' | '));
    assert.ok(csv.split(/\r?\n/).some(l => l.includes(c.first.replace(/,/g, ''))), 'в CSV число кэрри как на экране ' + c.first);
    // сброс — вид прежний
    await openMenu(pg);
    for (const k of ['carry', 'net', 'cdv']) { await fmtBtn(pg, k); await resetFmt(pg, k); }
    for (const k of ['carry', 'funding', 'net', 'cdv']) assert.deepStrictEqual(await colOf(pg, k), was[k], 'после сброса ' + k);
    assert.deepStrictEqual(errors, []);
  } finally { await browser.close(); }
});
