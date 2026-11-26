/* C02: шоки в координатах /risk, сумма Тейлора как в desk/scenario/taylor.py. */
(function () {
  'use strict';

  const KBD = 'RUB OFZ';  // опубликованный ID уровня КБД (core: RUB_KBD)
  const sum = xs => xs.reduce((s, x) => s + x, 0);

  function prepare(M, runPositions, payload) {
    const facts = new Map(runPositions.map(p => [p.id, p]));
    const risk = new Map((payload?.positions || []).map(p => [p.position_id, p]));
    const rubToRepr = 1 / (M.fx?.[M.repr] || 1);
    const bucketIndex = new Map(M.buckets.map((b, i) => [b, i]));
    const noTenor = M.buckets.length;  // последний столбец для вклада без весов по срокам
    const first = Math.log(M.pillars[0]), last = Math.log(M.pillars[noTenor - 1]);
    const twistWeights = M.pillars.map(t => (Math.log(t) - first) / (last - first) - 0.5);
    const factors = (payload?.factors || []).map(key => {
      const [kind, curve, bucket] = key.split('|');
      return kind === 'IR' ? { kind, curve, ccy: M.riskCurveCcy(curve), k: bucketIndex.get(bucket) } : { kind };
    });

    return function simulate(positions, sim, withConvexity) {
      const useGamma = !!(withConvexity && payload && payload.second_order !== false);
      const inScope = ccy => sim.scope === 'ALL' || sim.scope === ccy;
      const shifts = twistWeights.map(w => sim.par + sim.twist * w);
      const base = factors.map(f => f.kind === 'IR' && inScope(f.ccy) ? shifts[f.k] : 0);
      const rubBasis = inScope('RUB') ? sim.basis : 0;
      let linearOnly = 0;

      const rows = positions.map(p => {
        const fact = facts.get(p.id), row = risk.get(p.id);
        const byBucket = { r: Array(noTenor + 1).fill(0), b: Array(noTenor + 1).fill(0), c: Array(noTenor + 1).fill(0), cvx: Array(noTenor + 1).fill(0) };
        const out = { p, r: 0, b: 0, c: 0, cvx: null, t: 0, byBucket };
        if (p.riskProblem) return out;

        // Параллельный шок двигает и RUONIA, и уровень КБД. Базис = КБД − RUONIA.
        // «Базис ОФЗ» в обоих видах действует как «RUONIA + базис»: двигает КБД для ОФЗ и спред ОФЗ-ПК к RUONIA.
        // Прокси-кривые и ОФЗ-ИН с прокси по дюрации от этого сдвига не меняются.
        const hasOfzBasis = fact?.extra?.ofz_basis01 != null;
        const ofzInProxy = hasOfzBasis && p.group === 'GOV' && p.kind === 'IN' && p.quality !== 'exact';
        // У ОФЗ-ПК и старых ОФЗ собственный спред к RUONIA и есть их базис.
        // У ОФЗ на КБД собственный спред к ней держим постоянным.
        const spreadKind = row ? row.spread_kind : fact?.cs01 ? 'credit' : fact?.ofzSpr01 ? 'ofz' : null;
        const spreadBasis = spreadKind === 'ofz' && !hasOfzBasis;
        const kbdBasis = p.curveProxy || ofzInProxy || spreadBasis ? 0 : rubBasis;
        const spreadShift = spreadBasis ? rubBasis : spreadKind === 'credit' && inScope(p.ccy) ? sim.credit : 0;
        const shock = (f, i) => f.kind === 'CS' ? spreadShift : f.curve === KBD ? base[i] + kbdBasis : base[i];
        function spreadWeights() {
          if (!spreadBasis) return p.cs01b;
          const usesOfzSpreadRow = M.ofzView === 'kbd' && p.group === 'GOV' && p.kind === 'PK';
          return usesOfzSpreadRow ? p.ofzSprb : p.risk[KBD];
        }
        const add = (part, k, value) => { out[part] += value; byBucket[part][k] += value; };
        function distribute(part, value, weights) {
          const weight = sum(weights);
          if (!weight) byBucket[part][noTenor] += value;
          else weights.forEach((w, k) => { byBucket[part][k] += value * (w / weight); });
        }
        function addCurve(curve, k, dv, rateShift) {
          const riskInRepr = dv * rubToRepr;
          add('r', k, riskInRepr * rateShift);
          if (curve === KBD) add('b', k, riskInRepr * kbdBasis);
        }
        function addSpread(dv) {
          const part = spreadBasis ? 'b' : 'c', value = (dv * rubToRepr) * spreadShift;
          if (value === 0) return;
          out[part] += value;
          distribute(part, value, spreadWeights());
        }

        if (row) {
          for (const [i, dv] of row.delta) {
            const f = factors[i];
            if (f.kind === 'IR') addCurve(f.curve, f.k, dv, base[i]);
            else if (f.kind === 'CS') addSpread(dv);
          }
          if (useGamma && row.gamma.length) {
            const x = factors.map(shock);
            out.cvx = 0;
            for (const [i, j, v] of row.gamma) {
              const term = (i === j ? 0.5 : 1) * (v * rubToRepr) * x[i] * x[j];
              if (term === 0) continue;
              out.cvx += term;
              const a = factors[i].k, b = factors[j].k;
              if (a == null && b == null) distribute('cvx', term, spreadWeights());
              else if (a == null || b == null || a === b) byBucket.cvx[a ?? b] += term;
              else { byBucket.cvx[a] += term / 2; byBucket.cvx[b] += term / 2; }
            }
          } else if (useGamma && row.delta.some(([i]) => shock(factors[i], i) !== 0)) linearOnly++;
        } else if (!payload && fact) {
          // Пока /risk не загружен, линейная часть доступна из тех же фактов прогона.
          for (const [curve, values] of Object.entries(fact.risk || {})) {
            const scope = inScope(M.riskCurveCcy(curve));
            values.forEach((dv, k) => { if (dv) addCurve(curve, k, dv, scope ? shifts[k] : 0); });
          }
          addSpread(fact.cs01 || fact.ofzSpr01 || 0);
        } else if (!payload) {
          // Демо уже хранит риск ставок и базиса отдельными рядами в валюте отчёта.
          for (const curve of M.curves) {
            if (curve.kind === 'rate' && inScope(curve.ccy)) p.risk[curve.id].forEach((dv, k) => add('r', k, dv * shifts[k]));
          }
          p.risk[KBD].forEach((dv, k) => add('b', k, dv * rubBasis));
          if (inScope(p.ccy)) p.cs01b.forEach((dv, k) => add('c', k, dv * sim.credit));
        }
        out.t = out.r + out.b + out.c + (out.cvx ?? 0);
        return out;
      });
      return { rows, shifts, linearOnly };
    };
  }

  if (typeof module !== 'undefined' && module.exports) module.exports = { prepare };
  else window.C02Sim = { prepare };
})();
