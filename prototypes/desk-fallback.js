// Сервер не отдал данные деска (window.DESK нет):
//  - в базе нет прогонов (сервер ставит window.DESK_EMPTY) — экран «Прогонов ещё нет» с ходом автопилота
//    (desk-empty.js), а не демо-набор; data.js грузится только чтобы код страницы не упал, он скрыт под экраном;
//  - явный демо-режим (?demo=1) или статический хостинг без API — демо-набор data.js, как раньше.
if (!window.DESK) document.write('<script src="data.js"><\/script>' + (window.DESK_EMPTY ? '<script src="desk-empty.js"><\/script>' : ''));
