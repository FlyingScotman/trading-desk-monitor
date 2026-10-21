// Миниатюры для галереи: node prototypes/concepts/tools/thumbs.js [page.html ...] → prototypes/concepts/thumbs/*.jpg
const { chromium } = require('/opt/node22/lib/node_modules/playwright');
const path = require('path'), fs = require('fs');
const dir = path.resolve(__dirname, '..'), out = path.join(dir, 'thumbs');
fs.mkdirSync(out, { recursive: true });
let pages = process.argv.slice(2);
if (!pages.length) pages = fs.readdirSync(dir).filter(f => /^c\d+.*\.html$/.test(f));
(async () => {
  const b = await chromium.launch();
  for (const name of pages) {
    const ctx = await b.newContext({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 0.5 });
    const pg = await ctx.newPage();
    await pg.goto('file://' + path.join(dir, name)); await pg.waitForTimeout(1200);
    await pg.screenshot({ path: path.join(out, name.replace('.html', '.jpg')), type: 'jpeg', quality: 72 });
    await ctx.close(); console.log('thumb', name);
  }
  await b.close();
})();
