/* C19 «PnL за период», экран D · Ставки по кривым: placeholder until TDM#409 replaces it (same registration, same ctx). */
(function () {
  'use strict';
  window.PNL_PERIOD_SCREENS = window.PNL_PERIOD_SCREENS || {};
  PNL_PERIOD_SCREENS.curves = {
    render(main) {
      main.innerHTML = '<div class="k-panel"><div class="hd"><h3>D · Ставки по кривым</h3></div><div class="bd"><div class="k-empty">экран в работе</div></div></div>';
    },
    dispose() { },
  };
})();
