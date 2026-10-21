// Смоук-проверка концептов: открыть каждую страницу, собрать ошибки консоли, снять скриншот,
// щёлкнуть первую кликабельную цифру (разбор) и первую строку позиции (карточка).
// node prototypes/concepts/tools/smoke.js [out_dir] [page.html ...]
const { chromium } = require('/opt/node22/lib/node_modules/playwright');
const path = require('path'), fs = require('fs');
const dir = path.resolve(__dirname, '..');
const out = path.resolve(process.argv[2] || path.join(dir, '.shots'));
fs.mkdirSync(out, { recursive: true });
let pages = process.argv.slice(3);
if (!pages.length) pages = fs.readdirSync(dir).filter(f => /\.html$/.test(f));
(async () => {
  const b = await chromium.launch();
  let bad = 0;
  for (const name of pages) {
    const pg = await b.newPage({ viewport: { width: 1600, height: 1000 } });
    const errs = [];
    pg.on('pageerror', e => errs.push('pageerror: ' + e.message));
    pg.on('console', m => { if (m.type() === 'error') errs.push('console: ' + m.text()); });
    await pg.goto('file://' + path.join(dir, name));
    await pg.waitForTimeout(900);
    await pg.screenshot({ path: path.join(out, name.replace('.html', '.png')) });
    const dr = await pg.$('[data-dr]');
    if (dr) { await dr.click(); await pg.waitForTimeout(300); await pg.screenshot({ path: path.join(out, name.replace('.html', '-drill.png')) }); await pg.keyboard.press('Escape'); }
    const row = await pg.$('tr.row[data-id^="P"]');
    if (row) { await row.click(); await pg.waitForTimeout(500); await pg.screenshot({ path: path.join(out, name.replace('.html', '-card.png')) }); await pg.keyboard.press('Escape'); }
    console.log((errs.length ? 'FAIL ' : 'ok   ') + name + (dr ? ' drill' : ' -') + (row ? ' card' : ' -') + (errs.length ? '\n   ' + errs.slice(0, 5).join('\n   ') : ''));
    if (errs.length) bad++;
    await pg.close();
  }
  await b.close();
  process.exit(bad ? 1 : 0);
})();
