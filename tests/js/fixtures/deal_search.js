'use strict';

const DEAL = '88000101';
const IDS = ['TEST-A|88000901', 'TEST-B|88000901', 'TEST-A|88000902'];

function dealSearchRun(D) {
  const base = D.positions.find(p => p.instrType === 'FX');
  const extras = [
    { reported: { deal_id: Number(DEAL) } },
    { core: { trades: [DEAL, '88000102'] } },
    { legs: [{ trade_id: '88000103' }], repos: [{ trade_id: '88000104' }] },
  ];
  D.positions = IDS.map((id, i) => ({ ...base, id, book: id.split('|')[0], name: 'Тестовая позиция', extra: extras[i] }));
}

module.exports = { DEAL, IDS, dealSearchRun };
