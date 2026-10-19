// Number formatting as in prototypes/scenario-lab.html: ru-RU, real minus sign, values in млн ₽.
const NF: Record<number, Intl.NumberFormat> = {};
const nf = (d: number) => (NF[d] ??= new Intl.NumberFormat('ru-RU', { minimumFractionDigits: d, maximumFractionDigits: d }));

export function num(x: number, d = 0): string {
  if (Math.round(Math.abs(x) * 10 ** d) === 0) x = 0;
  return nf(d).format(x).replace(/-/g, '−');
}
export function snum(x: number, d = 0): string {
  const s = num(x, d);
  return x > 0 && s !== num(0, d) ? '+' + s : s;
}
export const smln = (x: number | null | undefined, d = 2) => (x == null ? '—' : snum(x / 1e6, d));
