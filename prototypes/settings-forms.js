/*
 * Формы настроек адаптера (08 §4 settings_tabs, FND-4, R40): юрлица, книги, фондирование, источники — таблицами и полями
 * по JSON Schema, которую описывает сам адаптер (describe()["settings_tabs"]), а не правкой JSON руками.
 *
 * Чистая часть (её проверяет tests/js/settings_forms.test.js, без браузера):
 *   SF.layout(schema)        — что рисовать: таблицы (массивы объектов), группы полей (объекты), отдельные поля;
 *   SF.columns(item)         — столбцы таблицы: вид поля (enum, bool, date, amount, integer, number, text, codes, strlist);
 *   SF.optionsFor(col, L)    — выпадающий список поля: enum схемы, юрлица, книги, валюты, индексы хаба;
 *   SF.validate(schema, d)   — та же проверка, что на сервере (desk.settings_forms), с кодами ошибок адаптера;
 *   SF.say(issue, schema)    — текст ошибки по-русски; SF.issueFile(issue) — к какому файлу относится замечание адаптера;
 *   SF.diff(a, b), SF.hunks  — построчное сравнение «было → станет» для предпросмотра перед сохранением;
 *   SF.unconfigured(doc, r)  — книги из недавних позиций, которых нет в books.json («не настроено»), SF.bookPrefill;
 *   SF.ruleGroups, SF.rateText, SF.fundingMatrix — история ставок фондирования и сводка «тип книги × валюта».
 * DOM-часть: new SF.Form({...}) — редактор одного файла (таблицы, правка строки в выезжающей панели, «Эксперт» — сырой
 * JSON, проверка на сервере с предпросмотром изменений, сохранение, история правок). Данные: /api/v1/adapter/settings/*,
 * /api/v1/adapter/config/* (settings_routes.py).
 */
(function (root) {
  'use strict';
  var SF = {};

  /* ---------- коды замечаний → по-русски (коды читателей адаптера, tda.config, и проверок сервера) ---------- */
  SF.CODES = {
    missing_required: 'обязательное поле не заполнено',
    bad_value: 'недопустимое значение',
    bad_date: 'нужна дата (ГГГГ-ММ-ДД)',
    bad_number: 'нужно число',
    unknown_field: 'поле не из формы — адаптер его отбросит',
    unknown_config_key: 'ключ не из формы — адаптер его не читает',
    bad_config: 'файл не читается: нужен JSON-объект нужного вида',
    reserved_key: 'ключи, начинающиеся с «_», — только комментарии',
    duplicate_key: 'повтор: та же запись с той же датой «действует с» — не возьмётся ни одна из копий',
    bad_book_codes: 'коды книги: источник (или *) → список непустых кодов',
    book_code_conflict: 'один код отчёта ведёт в две книги одного юрлица',
    unknown_book_field: 'поле книги не из контракта — будет отброшено',
    unknown_entity_field: 'поле юрлица не из контракта — будет отброшено',
    unknown_rule_field: 'поле правила не из контракта — будет отброшено',
    unknown_ocp_to: 'ОВП переносится в книгу, которой нет в списке книг',
    unknown_entity: 'такого юрлица нет в списке юрлиц',
    unknown_book: 'такой книги нет в списке книг',
    ambiguous_rule: 'два правила с одинаковыми селекторами и датой — какое брать, неясно',
    bad_funding_draft: 'доп. спреды или политика РЕПО заполнены неверно',
    funding_draft_not_sent: 'доп. спреды и политика РЕПО пока не передаются в монитор',
    placeholder_books: 'нет папки настроек: адаптер берёт учебные книги',
    no_books: 'файла книг нет',
    auto_book: 'книга из отчёта, не описанная в books.json (авто-книга)',
    bad_options: 'параметр неверного типа — взято значение по умолчанию',
    options_unknown_key: 'неизвестный параметр',
    bad_reference: 'адаптер вернул справочник неверного вида',
    needs_index: 'для «индекс + спред» нужен индекс',
    needs_fixed_rate: 'для фиксированной ставки нужна ставка, %'
  };
  SF.SEVERITY = { error: 'ошибка', warning: 'предупреждение', info: 'к сведению' };
  SF.VIA = { form: 'форма', raw: 'JSON (эксперт)', file: 'вне программы' };

  /* ---------- схема ---------- */
  function types(node) {
    var t = node && node.type;
    return typeof t === 'string' ? [t] : Array.isArray(t) ? t : [];
  }
  SF.types = types;
  function has(node, t) { return types(node).indexOf(t) >= 0; }
  function isObj(v) { return v !== null && typeof v === 'object' && !Array.isArray(v); }

  // вид поля: как его показать и как разобрать ввод
  SF.kind = function (node) {
    node = node || {};
    if (Array.isArray(node.enum)) return 'enum';
    if (node.format === 'date') return 'date';
    if (has(node, 'boolean')) return 'bool';
    if (has(node, 'number') && has(node, 'string')) return 'amount';
    if (has(node, 'integer')) return 'integer';
    if (has(node, 'number')) return 'number';
    if (has(node, 'array')) return node.items && has(node.items, 'object') ? 'table' : 'strlist';
    if (has(node, 'object')) {
      var ap = node.additionalProperties;
      if (isObj(ap) && has(ap, 'array') && !node.properties) return 'codes';
      return 'object';
    }
    return 'text';
  };

  // что рисовать в файле: таблицы (массивы объектов), группы полей (объекты), отдельные поля верхнего уровня
  SF.layout = function (schema) {
    var props = (schema && schema.properties) || {}, req = (schema && schema.required) || [];
    var out = { tables: [], objects: [], fields: [] };
    Object.keys(props).forEach(function (key) {
      var node = props[key], k = SF.kind(node), title = node.title || key;
      if (k === 'table') out.tables.push({ key: key, title: title, node: node, item: node.items, required: req.indexOf(key) >= 0 });
      else if (k === 'object' && node.properties) out.objects.push({ key: key, title: title, node: node });
      else out.fields.push(SF.column(key, node, req.indexOf(key) >= 0));
    });
    out.tables.sort(function (a, b) { return (b.required ? 1 : 0) - (a.required ? 1 : 0); });  // основная таблица — первой
    return out;
  };

  SF.column = function (key, node, required) {
    node = node || {};
    return { key: key, title: node.title || key, description: node.description || '', kind: SF.kind(node), node: node,
      required: !!required, enum: Array.isArray(node.enum) ? node.enum : null,
      default: node.default, nullable: has(node, 'null') };
  };

  // столбцы таблицы по схеме элемента, в порядке схемы
  SF.columns = function (item) {
    var props = (item && item.properties) || {}, req = (item && item.required) || [];
    return Object.keys(props).map(function (k) { return SF.column(k, props[k], req.indexOf(k) >= 0); });
  };

  /* ---------- выпадающие списки: поле → список (по имени поля, общему у адаптера и контракта 08 §10) ---------- */
  // L: {entities: [{value, label}], books: [...], currencies: [...], indices: [...]}
  SF.LIST_OF = { entity: 'entities', ocp_to: 'books', book_id: 'books', ccy: 'currencies', index_code: 'indices' };
  SF.optionsFor = function (col, L, table) {
    if (col.enum) return col.enum.filter(function (v) { return v !== null; }).map(function (v) { return { value: v, label: String(v) }; });
    var list = SF.LIST_OF[col.key];
    if (!list) return null;
    if (col.key === 'book_id' && table === 'books') return null;  // свой код книги, а не ссылка на книгу
    if (col.key === 'entity' && table === 'entities') return null;
    var opts = (L && L[list]) || [];
    return opts.length ? opts : null;
  };

  /* ---------- проверка (подмножество JSON Schema, как desk.settings_forms.check_schema) ---------- */
  function isType(v, t) {
    switch (t) {
      case 'null': return v === null;
      case 'boolean': return typeof v === 'boolean';
      case 'integer': return typeof v === 'number' && isFinite(v) && Math.floor(v) === v;
      case 'number': return typeof v === 'number' && isFinite(v);
      case 'string': return typeof v === 'string';
      case 'array': return Array.isArray(v);
      case 'object': return isObj(v);
    }
    return true;
  }
  function badCode(node) {
    if (node.format === 'date') return 'bad_date';
    if (has(node, 'number') || has(node, 'integer')) return 'bad_number';
    return 'bad_value';
  }
  function isDate(s) {
    if (!/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(s)) return false;
    var d = new Date(s + 'T00:00:00Z');
    return !isNaN(d) && d.toISOString().slice(0, 10) === s;
  }
  function re(p) { try { return new RegExp(p, 'u'); } catch (e) { return null; } }
  function nameOf(path) { for (var i = path.length - 1; i >= 0; i--) if (typeof path[i] === 'string') return path[i]; return 'файл'; }

  SF.validate = function (schema, data, max) {
    var out = [], limit = max || 500;
    function add(sev, code, path, msg) { if (out.length < limit) out.push({ severity: sev, code: code, path: path, message: msg, origin: 'schema' }); }
    function check(node, v, path) {
      if (!isObj(node)) return;
      var ts = types(node);
      if (ts.length && !ts.some(function (t) { return isType(v, t); })) { add('error', badCode(node), path, 'field ' + nameOf(path) + ': wrong type'); return; }
      if (Array.isArray(node.enum) && node.enum.indexOf(v) < 0) { add('error', 'bad_value', path, 'field ' + nameOf(path) + ': not one of the allowed values'); return; }
      if (typeof v === 'string') {
        var p = typeof node.pattern === 'string' ? re(node.pattern) : null;
        if (typeof node.minLength === 'number' && v.length < node.minLength) add('error', 'bad_value', path, 'field ' + nameOf(path) + ': empty');
        else if (p && !p.test(v)) add('error', badCode(node), path, 'field ' + nameOf(path) + ': does not match the format');
        else if (node.format === 'date' && !isDate(v)) add('error', 'bad_date', path, 'field ' + nameOf(path) + ': not a date');
      } else if (typeof v === 'number') {
        var b = [['minimum', function (x) { return v < x; }], ['exclusiveMinimum', function (x) { return v <= x; }],
                 ['maximum', function (x) { return v > x; }], ['exclusiveMaximum', function (x) { return v >= x; }]];
        for (var i = 0; i < b.length; i++) if (typeof node[b[i][0]] === 'number' && b[i][1](node[b[i][0]])) { add('error', 'bad_number', path, 'field ' + nameOf(path) + ': out of range'); break; }
      } else if (Array.isArray(v)) {
        v.forEach(function (x, j) { check(node.items, x, path.concat([j])); });
      } else if (isObj(v)) {
        var props = isObj(node.properties) ? node.properties : {}, pats = isObj(node.patternProperties) ? node.patternProperties : {};
        (node.required || []).forEach(function (k) { if (v[k] == null) add('error', 'missing_required', path.concat([k]), 'field ' + k + ': required'); });
        var names = node.propertyNames;
        Object.keys(v).forEach(function (k) {
          var x = v[k];
          if (isObj(names) && ((Array.isArray(names.enum) && names.enum.indexOf(k) < 0) || (typeof names.pattern === 'string' && !re(names.pattern).test(k)))) {
            add('error', 'bad_value', path.concat([k]), 'key of ' + nameOf(path) + ': not one of the allowed names'); return;
          }
          var sub = props[k];
          if (x == null && !(sub && has(sub, 'null'))) return;  // пусто и null — то же, что поля нет
          if (sub) { check(sub, x, path.concat([k])); return; }
          var matched = Object.keys(pats).filter(function (p) { var r = re(p); return r && r.test(k); });
          if (matched.length) { matched.forEach(function (p) { check(pats[p], x, path.concat([k])); }); return; }
          var extra = node.additionalProperties === undefined ? true : node.additionalProperties;
          if (extra === false) add('warning', path.length ? 'unknown_field' : 'unknown_config_key', path.concat([k]), nameOf(path) + ': a key outside the form is ignored');
          else if (isObj(extra)) check(extra, x, path.concat([k]));
        });
      }
    }
    if (!isObj(data)) add('error', 'bad_config', [], 'the file must be a JSON object');
    else check(schema, data, []);
    return out.concat(SF.ruleHints(schema, data));
  };

  // правила адаптера сверх схемы, которые полезно видеть сразу у поля (tda.config._rule_problem): ставка «индекс + спред»
  // без индекса, фиксированная без ставки. Сервер их скажет при проверке; здесь — те же коды, что у адаптера
  SF.ruleHints = function (schema, data) {
    var out = [];
    if (!isObj(data) || !Array.isArray(data.rules)) return out;
    var item = schema && schema.properties && schema.properties.rules && schema.properties.rules.items;
    if (!item || !item.properties || !item.properties.rate_kind) return out;
    data.rules.forEach(function (r, i) {
      if (!isObj(r)) return;
      if (r.rate_kind === 'index_spread' && r.index_code == null) out.push({ severity: 'error', code: 'missing_required', hint: 'needs_index', path: ['rules', i, 'index_code'], message: 'rule: index_spread needs index_code', origin: 'schema' });
      if (r.rate_kind === 'fixed' && r.fixed_rate_pct == null) out.push({ severity: 'error', code: 'missing_required', hint: 'needs_fixed_rate', path: ['rules', i, 'fixed_rate_pct'], message: 'rule: fixed needs fixed_rate_pct', origin: 'schema' });
    });
    return out;
  };

  // заголовок поля по пути в схеме
  SF.titleAt = function (schema, path) {
    var node = schema, title = null;
    for (var i = 0; i < (path || []).length && node; i++) {
      var p = path[i];
      if (typeof p === 'number') { node = node.items; continue; }
      node = (node.properties && node.properties[p]) || null;
      if (node && node.title) title = node.title;
    }
    return title;
  };
  SF.say = function (issue, schema) {
    var text = SF.CODES[issue.hint] || SF.CODES[issue.code] || issue.message || issue.code;
    var t = issue.path ? SF.titleAt(schema, issue.path) : (issue.field ? SF.titleAt(schema, [Object.keys((schema && schema.properties) || {})[0], 0, issue.field]) || issue.field : null);
    return t ? t + ': ' + text : text;
  };
  SF.pathKey = function (path) { return (path || []).join('.'); };

  // к какому файлу относится замечание reference() (у адаптера в замечании нет имени файла: по коду и началу текста)
  var BOOK_CODES = ['bad_book_codes', 'book_code_conflict', 'unknown_book_field', 'unknown_ocp_to', 'placeholder_books', 'no_books', 'auto_book'];
  SF.issueFile = function (i) {
    var c = i.code || '', m = i.message || '';
    if (/^entit/.test(m) || c === 'unknown_entity_field') return 'entities.json';
    if (/^(rule|funding)/.test(m) || c === 'unknown_rule_field' || /funding/.test(c) || c === 'ambiguous_rule') return 'funding.json';
    if (/^options/.test(m) || c === 'bad_options' || c === 'options_unknown_key') return 'options.json';
    if (/^(field |a book|book|codes|a local code)/.test(m) || BOOK_CODES.indexOf(c) >= 0) return 'books.json';
    return null;
  };

  /* ---------- значения ---------- */
  SF.parseAmount = function (text) {
    var t = String(text == null ? '' : text).trim().replace(/\s+/g, '').replace(',', '.');
    if (!t) return undefined;
    if (/^-?[0-9]+(\.[0-9]+)?$/.test(t)) { var n = Number(t); return String(n) === t ? n : t; }  // 1.50 — строкой, без потерь
    return t;  // неверное — как есть: проверка покажет «нужно число»
  };
  SF.parseNumber = function (text, integer) {
    var t = String(text == null ? '' : text).trim().replace(',', '.');
    if (!t) return undefined;
    var n = Number(t);
    return isFinite(n) && (!integer || Math.floor(n) === n) ? n : t;
  };
  // коды книги в отчётах: «positions_daily: A, B; *: C» ⇄ {"positions_daily": ["A","B"], "*": ["C"]}
  SF.fmtCodes = function (codes) {
    if (!isObj(codes)) return '';
    return Object.keys(codes).map(function (k) { return k + ': ' + (codes[k] || []).join(', '); }).join('; ');
  };
  SF.parseCodes = function (text) {
    var t = String(text || '').trim();
    if (!t) return undefined;
    var out = {};
    t.split(';').forEach(function (part) {
      part = part.trim(); if (!part) return;
      var i = part.indexOf(':'), k = i < 0 ? '*' : part.slice(0, i).trim(), v = i < 0 ? part : part.slice(i + 1);
      out[k] = (out[k] || []).concat(v.split(',').map(function (s) { return s.trim(); }).filter(Boolean));
    });
    return out;
  };
  SF.fmtDate = function (s) { return typeof s === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(s) ? s.slice(8, 10) + '.' + s.slice(5, 7) + '.' + s.slice(0, 4) : s; };
  SF.display = function (col, v) {
    if (v === undefined || v === null || v === '') return '';
    if (col.kind === 'bool') return v === true ? 'да' : v === false ? 'нет' : String(v);
    if (col.kind === 'date') return SF.fmtDate(v);
    if (col.kind === 'codes') return SF.fmtCodes(v);
    if (Array.isArray(v)) return v.join(', ');
    if (isObj(v)) return JSON.stringify(v);
    return String(v);
  };
  // канонический JSON (ключи по алфавиту) — сравнить «изменено ли» независимо от порядка ключей
  SF.canon = function (v) {
    if (Array.isArray(v)) return '[' + v.map(SF.canon).join(',') + ']';
    if (isObj(v)) return '{' + Object.keys(v).sort().filter(function (k) { return v[k] !== undefined; }).map(function (k) { return JSON.stringify(k) + ':' + SF.canon(v[k]); }).join(',') + '}';
    return JSON.stringify(v === undefined ? null : v);
  };
  SF.clone = function (v) { return v === undefined ? undefined : JSON.parse(JSON.stringify(v)); };
  // текст файла — как его запишет сервер (json.dumps(indent=2, ensure_ascii=False)); для сравнения достаточно
  SF.text = function (data) { return JSON.stringify(data, null, 2) + '\n'; };

  // пустой файл формы: обязательные списки — пустыми
  SF.empty = function (schema) {
    var out = {};
    ((schema && schema.required) || []).forEach(function (k) {
      var n = schema.properties && schema.properties[k];
      if (n) out[k] = has(n, 'array') ? [] : has(n, 'object') ? {} : undefined;
    });
    return out;
  };
  // новая строка таблицы: значения enum по умолчанию не подставляются (выбор — за человеком), кроме prefill
  SF.newRow = function (item, prefill) {
    var row = {};
    Object.keys(prefill || {}).forEach(function (k) { var v = prefill[k]; if (v !== undefined && v !== null && v !== '') row[k] = v; });
    return row;
  };

  /* ---------- книги из позиций, которых нет в books.json ---------- */
  SF.configuredIds = function (doc) {
    var ids = {};
    ((doc && doc.books) || []).forEach(function (b) {
      if (!isObj(b)) return;
      if (b.book_id) ids[b.book_id] = true;
      if (isObj(b.codes)) Object.keys(b.codes).forEach(function (k) { (b.codes[k] || []).forEach(function (c) { ids[c] = true; }); });
    });
    return ids;
  };
  SF.unconfigured = function (doc, recent) {
    var ids = SF.configuredIds(doc);
    return (recent || []).filter(function (r) { return r && r.book_id && !ids[r.book_id]; });
  };
  // строка books.json для книги из позиций: что известно из справочника сервера (авто-книга адаптера); остальное — человеку
  SF.bookPrefill = function (r) {
    var p = { book_id: r.book_id, name: r.name || r.book_id, book_type: r.book_type || 'TRADING' };
    if (r.desk) p.desk = r.desk;
    if (r.entity) p.entity = r.entity;
    if (r.trader) p.trader = r.trader;
    var at = r.book_id.indexOf('@');  // авто-книга «код@юрлицо»: код отчёта — до «@»
    if (at > 0) p.codes = { '*': [r.book_id.slice(0, at)] };
    return p;
  };

  /* ---------- фондирование: история ставок и сводка ---------- */
  var SELECTORS = ['instrument_id', 'book_id', 'entity', 'book_type', 'ccy'];
  SF.ruleId = function (r) {  // как tda.config.rule_id_of
    if (r.rule_id) return r.rule_id;
    var sel = SELECTORS.slice().reverse().filter(function (k) { return r[k] != null; }).map(function (k) { return k + '=' + r[k]; });
    return 'auto:' + (sel.length ? sel.join(';') : 'default');
  };
  // правила по rule_id, внутри — по дате «действует с»; state: current (действует на today), past, future
  SF.ruleGroups = function (rules, today) {
    var by = {}, order = [];
    (rules || []).forEach(function (r, i) {
      if (!isObj(r)) return;
      var id = SF.ruleId(r);
      if (!by[id]) { by[id] = []; order.push(id); }
      by[id].push({ i: i, rule: r });
    });
    return order.map(function (id) {
      var rows = by[id].slice().sort(function (a, b) { return (a.rule.valid_from || '').localeCompare(b.rule.valid_from || '') || a.i - b.i; });
      var cur = -1;
      rows.forEach(function (x, j) { if (!x.rule.valid_from || x.rule.valid_from <= today) cur = j; });
      rows.forEach(function (x, j) {
        x.state = j === cur ? 'current' : j < cur ? 'past' : 'future';
        x.until = j + 1 < rows.length ? rows[j + 1].rule.valid_from : null;
      });
      return { id: id, rows: rows, current: cur >= 0 ? rows[cur].rule : null };
    });
  };
  function num(v) { var s = String(v); return s.replace('.', ','); }
  SF.rateText = function (r) {
    if (!isObj(r)) return '';
    switch (r.rate_kind) {
      case 'index_spread': {
        var sp = r.spread_bp == null ? 0 : Number(r.spread_bp);
        return (r.index_code || '?') + (sp ? (sp < 0 ? ' − ' : ' + ') + num(Math.abs(sp)) + ' б.п.' : '');
      }
      case 'fixed': return r.fixed_rate_pct == null ? 'фикс ?' : num(r.fixed_rate_pct) + ' % фикс';
      case 'contract': return 'контрактная';
      case 'none': return 'не фондируется';
    }
    return r.rate_kind || '';
  };
  SF.BASIS = { mv_clean: 'MV без НКД', mv_dirty: 'MV с НКД', cash: 'кэш-баланс', cost: 'стоимость покупки', none: 'не фондируется' };
  // сводка: тип книги × валюта, ставка правила, действующего сегодня, только правил без юрлица/книги/бумаги
  SF.fundingMatrix = function (rules, today, bookTypes) {
    var groups = SF.ruleGroups(rules, today), ccys = {}, cells = {};
    groups.forEach(function (g) {
      var r = g.current;
      if (!r || r.active === false || r.entity || r.book_id || r.instrument_id) return;
      var c = r.ccy ? String(r.ccy).toUpperCase() : '*', t = r.book_type || '*';
      ccys[c] = true;
      cells[t + '|' + c] = { text: SF.rateText(r), basis: r.basis, rule: r, id: g.id, history: g.rows.length };
    });
    var cols = Object.keys(ccys).sort(function (a, b) { return a === '*' ? 1 : b === '*' ? -1 : a === 'RUB' ? -1 : b === 'RUB' ? 1 : a.localeCompare(b); });
    var rows = (bookTypes || []).slice();
    Object.keys(cells).forEach(function (k) { var t = k.split('|')[0]; if (rows.indexOf(t) < 0) rows.push(t); });
    return { rows: rows, cols: cols, cell: function (t, c) { return cells[t + '|' + c] || null; },
      special: groups.filter(function (g) { var r = g.current; return r && (r.entity || r.book_id || r.instrument_id); }) };
  };

  /* ---------- сравнение строк ---------- */
  SF.diff = function (a, b) {
    var A = String(a || '').split('\n'), B = String(b || '').split('\n');
    if (A[A.length - 1] === '') A.pop();
    if (B[B.length - 1] === '') B.pop();
    var pre = 0;
    while (pre < A.length && pre < B.length && A[pre] === B[pre]) pre++;
    var suf = 0;
    while (suf < A.length - pre && suf < B.length - pre && A[A.length - 1 - suf] === B[B.length - 1 - suf]) suf++;
    var a2 = A.slice(pre, A.length - suf), b2 = B.slice(pre, B.length - suf), mid = [];
    var n = a2.length, m = b2.length;
    if (n * m > 4e6) {  // слишком много — «всё удалено, всё добавлено»
      a2.forEach(function (t) { mid.push({ op: '-', text: t }); });
      b2.forEach(function (t) { mid.push({ op: '+', text: t }); });
    } else {
      var L = [], i, j;
      for (i = 0; i <= n; i++) { L.push(new Int32Array(m + 1)); }
      for (i = n - 1; i >= 0; i--) for (j = m - 1; j >= 0; j--) L[i][j] = a2[i] === b2[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
      i = 0; j = 0;
      while (i < n || j < m) {
        if (i < n && j < m && a2[i] === b2[j]) { mid.push({ op: ' ', text: a2[i] }); i++; j++; }
        else if (j < m && (i >= n || L[i][j + 1] >= L[i + 1][j])) { mid.push({ op: '+', text: b2[j] }); j++; }
        else { mid.push({ op: '-', text: a2[i] }); i++; }
      }
    }
    var out = A.slice(0, pre).map(function (t) { return { op: ' ', text: t }; }).concat(mid, A.slice(A.length - suf).map(function (t) { return { op: ' ', text: t }; }));
    var na = 0, nb = 0;
    out.forEach(function (x) { if (x.op !== '+') x.a = ++na; if (x.op !== '-') x.b = ++nb; });
    return out;
  };
  // только изменённые места с context строками вокруг; пропуски — {op: '…', skipped: n}
  SF.hunks = function (lines, context) {
    var c = context == null ? 3 : context, keep = lines.map(function () { return false; });
    lines.forEach(function (x, i) { if (x.op !== ' ') for (var j = Math.max(0, i - c); j <= Math.min(lines.length - 1, i + c); j++) keep[j] = true; });
    var out = [], skip = 0;
    lines.forEach(function (x, i) {
      if (keep[i]) { if (skip) out.push({ op: '…', skipped: skip }); skip = 0; out.push(x); } else skip++;
    });
    if (skip) out.push({ op: '…', skipped: skip });
    return out;
  };
  SF.diffStats = function (lines) {
    var s = { added: 0, removed: 0 };
    lines.forEach(function (x) { if (x.op === '+') s.added++; else if (x.op === '-') s.removed++; });
    return s;
  };

  /* ================= DOM ================= */
  function h(tag, attrs) {
    var el = document.createElement(tag), kids = Array.prototype.slice.call(arguments, 2);
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (v == null || v === false) return;
      if (k === 'class') el.className = v;
      else if (k.slice(0, 2) === 'on') el.addEventListener(k.slice(2), v);
      else if (k === 'value') el.value = v;
      else if (k === 'checked') el.checked = !!v;
      else if (k === 'text') el.textContent = v;
      else el.setAttribute(k, v === true ? '' : v);
    });
    (function add(list) {
      list.forEach(function (c) {
        if (c == null || c === false) return;
        if (Array.isArray(c)) add(c); else el.append(c.nodeType ? c : String(c));
      });
    })(kids);
    return el;
  }
  SF.h = h;
  function today() { var d = new Date(); return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10); }
  SF.today = today;
  function when(s) { return s ? new Date(s).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' }) : ''; }

  // поле ввода по виду; set(v) — undefined убирает ключ
  SF.control = function (col, value, set, L, table) {
    var opts = SF.optionsFor(col, L, table), id = 'sf-' + Math.random().toString(36).slice(2, 9);
    var el;
    if (col.kind === 'bool') {
      // без значения и без default в схеме — «как по умолчанию у адаптера»: не выдаём это за «нет»
      var def = col.default === true, unset = value === undefined && col.default === undefined, on = value === undefined ? def : value === true;
      var cb = h('input', { type: 'checkbox', id: id, checked: on, onchange: function () { set(cb.checked === def && value === undefined && !unset ? undefined : cb.checked); } });
      el = h('label', { class: 'tgl' }, cb, h('span', { class: 'tgl-s' + (unset ? ' unset' : '') }), h('span', { class: 'tgl-l' }, unset ? 'не задано — как у адаптера по умолчанию' : on ? 'да' : 'нет'));
      cb.addEventListener('change', function () { el.lastChild.textContent = cb.checked ? 'да' : 'нет'; el.children[1].className = 'tgl-s'; });
      el.dataset.input = id;
      return el;
    }
    if (opts) {
      var sel = h('select', { class: 'inp', id: id });
      sel.append(h('option', { value: '' }, col.required ? '— выберите —' : '—'));
      var known = false;
      opts.forEach(function (o) {
        if (o.value === value) known = true;
        sel.append(h('option', { value: String(o.value), title: o.title || null }, o.label && o.label !== String(o.value) ? o.value + ' — ' + o.label : String(o.value)));
      });
      if (value != null && value !== '' && !known) sel.append(h('option', { value: String(value) }, value + ' (нет в списке)'));
      if (!col.enum) sel.append(h('option', { value: '\u0000other' }, 'другое значение…'));
      sel.value = value == null ? '' : String(value);
      sel.addEventListener('change', function () {
        if (sel.value === '\u0000other') {
          var t = root.prompt ? root.prompt((col.title || col.key) + ':', value == null ? '' : value) : null;
          if (t == null) { sel.value = value == null ? '' : String(value); return; }
          t = t.trim();
          if (t && !Array.prototype.some.call(sel.options, function (o) { return o.value === t; })) sel.insertBefore(h('option', { value: t }, t), sel.lastChild);
          sel.value = t; value = t || undefined; set(value);
          return;
        }
        var v = sel.value;
        if (col.enum) { var hit = col.enum.filter(function (e) { return String(e) === v; }); set(v === '' ? undefined : hit.length ? hit[0] : v); }
        else set(v === '' ? undefined : v);
        value = v === '' ? undefined : v;
      });
      return sel;
    }
    if (col.kind === 'strlist') {
      var ta = h('textarea', { class: 'inp ta-s', id: id, rows: 3, spellcheck: 'false', placeholder: 'по одному в строке' });
      ta.value = Array.isArray(value) ? value.join('\n') : '';
      ta.addEventListener('input', function () { var l = ta.value.split('\n').map(function (s) { return s.trim(); }).filter(Boolean); set(l.length ? l : undefined); });
      return ta;
    }
    var attrs = { class: 'inp', id: id, autocomplete: 'off', spellcheck: 'false' };
    if (col.kind === 'date') attrs.type = 'date';
    if (col.kind === 'amount' || col.kind === 'number' || col.kind === 'integer') { attrs.inputmode = 'decimal'; attrs.class = 'inp mono num'; }
    if (col.kind === 'codes') { attrs.class = 'inp mono'; attrs.placeholder = 'источник: код, код; *: код'; }
    if (col.key === 'ccy') { attrs.class = 'inp mono'; attrs.maxlength = 3; }
    el = h('input', attrs);
    el.value = col.kind === 'codes' ? SF.fmtCodes(value) : value == null ? '' : typeof value === 'object' ? JSON.stringify(value) : String(value);
    el.addEventListener('input', function () {
      var t = el.value;
      if (col.kind === 'amount') set(SF.parseAmount(t));
      else if (col.kind === 'number' || col.kind === 'integer') set(SF.parseNumber(t, col.kind === 'integer'));
      else if (col.kind === 'codes') set(SF.parseCodes(t));
      else if (col.key === 'ccy') set(t.trim() ? t.trim().toUpperCase() : undefined);
      else set(t.trim() === '' ? undefined : t);
    });
    return el;
  };

  /*
   * Редактор одного файла по вкладке адаптера.
   * opts: {el, tab: {id, title, file, help, schema}, entry (config_files: template), api(method, url, body),
   *        lists() -> L, recent() -> [{book_id, ...}], onChange(form), onSaved(form), showIssue(text)}
   */
  SF.Form = function (opts) {
    this.o = opts; this.tab = opts.tab; this.schema = opts.tab.schema; this.file = opts.tab.file;
    this.L = SF.layout(this.schema);
    this.data = null; this.orig = null; this.sha = null; this.exists = false; this.text = null;
    this.expert = false; this.rawText = ''; this.rawError = null;
    this.issues = []; this.check = null; this.loadError = null; this.parseError = null;
  };
  var F = SF.Form.prototype;
  F.url = function (p) { return '/api/v1/adapter/' + p; };
  F.load = function () {
    var self = this;
    return this.o.api('GET', this.url('config/' + encodeURIComponent(this.file))).then(function (r) {
      self.loadError = null; self.exists = r.exists; self.sha = r.sha256 || null; self.text = r.text; self.parseError = null;
      var data = null;
      if (r.exists) {
        try { data = JSON.parse(r.text); } catch (e) { self.parseError = e.message; }
      }
      if (!r.exists || !isObj(data)) data = SF.empty(self.schema);
      self.orig = SF.clone(data); self.data = SF.clone(data); self.check = null;
      if (self.parseError) { self.expert = true; self.rawText = r.text || ''; self.rawError = 'Ошибка JSON: ' + self.parseError; }
      self.validate(); self.render();
    }, function (e) { self.loadError = e.message; self.render(); });
  };
  F.dirty = function () {
    if (this.expert && this.rawError) return this.rawText !== (this.text || '');
    return this.data != null && SF.canon(this.data) !== SF.canon(this.orig);
  };
  F.validate = function () {
    this.issues = this.data ? SF.validate(this.schema, this.data) : [];
    this.byPath = {};
    var self = this;
    this.issues.forEach(function (i) { var k = SF.pathKey(i.path); (self.byPath[k] = self.byPath[k] || []).push(i); });
    return this.issues;
  };
  F.errors = function () { return this.issues.filter(function (i) { return i.severity === 'error'; }); };
  F.changed = function () { this.check = null; this.validate(); if (this.o.onChange) this.o.onChange(this); };
  // замечания проверки сервера к строке таблицы (адаптер называет строку R<n>)
  F.adapterIssues = function () {
    var self = this;
    return ((this.check && this.check.adapter) || []).filter(function (i) { return SF.issueFile(i) === self.file; });
  };
  F.rowIssues = function (table, idx) {
    var pre = table + '.' + idx, out = [], self = this;
    Object.keys(this.byPath || {}).forEach(function (k) { if (k === pre || k.indexOf(pre + '.') === 0) out = out.concat(self.byPath[k]); });
    if (table === (this.L.tables[0] || {}).key) this.adapterIssues().forEach(function (i) { if (i.row === idx) out.push(i); });
    return out;
  };

  F.headEl = function () {
    var self = this;
    return h('div', { class: 'sf-head' },
      h('div', { class: 'sf-title' }, h('span', { class: 'mono sf-file' }, this.file),
        this.loadError ? h('span', { class: 'badge bad' }, 'не прочитан') :
          this.exists ? h('span', { class: 'badge ok' }, 'файл есть') : h('span', { class: 'badge' }, 'файла ещё нет'),
        this.dirty() ? h('span', { class: 'badge warn' }, 'изменено') : null,
        this.errors().length ? h('span', { class: 'badge bad' }, 'ошибок: ' + this.errors().length) : null),
      h('div', { class: 'sf-acts' },
        h('label', { class: 'chk', title: 'Сырой JSON файла — для того, чего нет в форме' },
          h('input', { type: 'checkbox', checked: this.expert, onchange: function (e) { self.setExpert(e.target.checked); } }), 'Эксперт'),
        h('button', { class: 'ibtn', type: 'button', onclick: function () { self.history(); } }, 'История'),
        h('button', { class: 'ibtn', type: 'button', disabled: !this.dirty(), onclick: function () { self.revert(); } }, 'Отменить'),
        h('button', { class: 'btn', type: 'button', disabled: !this.dirty() && this.exists, onclick: function () { self.review(); } }, 'Проверить и сохранить…')));
  };
  // значки и кнопки шапки — без перерисовки полей (фокус и курсор остаются)
  F.refreshHead = function () {
    var head = this.o.el.querySelector('.sf-head');
    if (head) head.replaceWith(this.headEl());
    if (this.o.onChange) this.o.onChange(this);
  };
  F.render = function () {
    var el = this.o.el, self = this;
    el.textContent = '';
    el.classList.add('sf');
    el.append(this.headEl());
    if (this.loadError) { el.append(h('div', { class: 'note err' }, 'Не удалось прочитать файл: ' + this.loadError)); return; }
    if (this.expert) { el.append(this.renderRaw()); return; }
    if (!this.exists && this.o.entry && this.o.entry.template && SF.canon(this.data) === SF.canon(this.orig)) {
      el.append(h('div', { class: 'note' }, 'Файла ещё нет — адаптер работает без него. Заполните таблицу или ',
        h('button', { class: 'lnk', type: 'button', onclick: function () { self.data = SF.clone(self.o.entry.template); self.changed(); self.render(); } }, 'вставьте пример'),
        ' и сохраните.'));
    }
    var extra = this.tab.id === 'funding' ? this.renderFundingSummary() : null;
    if (extra) el.append(extra);
    this.L.tables.forEach(function (t, n) { el.append(self.renderTable(t, n === 0)); });
    if (this.L.fields.length) el.append(this.renderFields(this.L.fields, this.data, function () { self.changed(); self.refreshHead(); }, null));
    this.L.objects.forEach(function (o) { el.append(self.renderObject(o)); });
    var rest = this.issues.filter(function (i) { return !i.path || i.path.length < 2; });
    if (rest.length) el.append(h('ul', { class: 'sf-issues' }, rest.map(function (i) { return self.issueLi(i); })));
  };
  F.issueLi = function (i) {
    return h('li', { class: 'sev-' + i.severity },
      h('span', { class: 'badge ' + (i.severity === 'error' ? 'bad' : i.severity === 'warning' ? 'warn' : '') }, SF.SEVERITY[i.severity] || i.severity),
      ' ', SF.say(i, this.schema), ' ', h('span', { class: 'mono mut3 code' }, i.code));
  };

  F.visibleCols = function (t) {
    var cols = SF.columns(t.item), rows = this.data[t.key] || [], funding = cols.some(function (c) { return c.key === 'rate_kind'; });
    var shown = cols.filter(function (c) {
      if (funding && ['index_code', 'spread_bp', 'fixed_rate_pct', 'rate_kind'].indexOf(c.key) >= 0) return false;
      return c.required || rows.some(function (r) { return isObj(r) && r[c.key] !== undefined && r[c.key] !== null; });
    });
    if (funding) {
      var at = shown.findIndex(function (c) { return c.key === 'basis'; });
      shown.splice(at < 0 ? shown.length : at + 1, 0, { key: '_rate', title: 'Ставка', kind: 'rate', virtual: true });
    }
    return { all: cols, shown: shown };
  };
  F.renderTable = function (t, primary) {
    var self = this, rows = this.data[t.key] || [], vc = this.visibleCols(t), hidden = vc.all.length - vc.shown.filter(function (c) { return !c.virtual; }).length;
    var tbody = h('tbody');
    var groups = this.tab.id === 'funding' && t.key === 'rules' ? SF.ruleGroups(rows, today()) : null;
    var state = {};
    if (groups) groups.forEach(function (g) { g.rows.forEach(function (x) { state[x.i] = x; }); });
    var order = groups ? [].concat.apply([], groups.map(function (g) { return g.rows.map(function (x) { return x.i; }); })) : rows.map(function (_, i) { return i; });
    order.forEach(function (i) {
      var r = rows[i], iss = self.rowIssues(t.key, i), err = iss.some(function (x) { return x.severity === 'error'; });
      var st = state[i];
      var tr = h('tr', { class: 'row' + (err ? ' bad' : iss.length ? ' warn' : '') + (st && st.state !== 'current' ? ' dim' : ''), onclick: function () { self.editRow(t, i); } });
      vc.shown.forEach(function (c) {
        if (c.virtual) { tr.append(h('td', { class: 'mono' }, SF.rateText(r))); return; }
        var v = isObj(r) ? r[c.key] : undefined, bad = (self.byPath[t.key + '.' + i + '.' + c.key] || []).length || iss.some(function (x) { return x.field === c.key; });
        var td = h('td', { class: (bad ? 'bad ' : '') + (c.kind === 'enum' ? '' : c.kind === 'amount' || c.kind === 'number' || c.kind === 'integer' ? 'n mono' : c.key.indexOf('_id') > 0 || c.key === 'entity' ? 'mono' : '') },
          c.kind === 'enum' && v != null ? h('span', { class: 'tagc' }, String(v)) : SF.display(c, v));
        if (bad) td.title = iss.filter(function (x) { return (x.path && x.path[2] === c.key) || x.field === c.key; }).map(function (x) { return SF.say(x, self.schema); }).join('\n');
        tr.append(td);
      });
      tr.append(h('td', { class: 'sf-state' }, st ? h('span', { class: 'badge ' + (st.state === 'current' ? 'ok' : '') }, st.state === 'current' ? 'действует' : st.state === 'future' ? 'с ' + SF.fmtDate(st.rule.valid_from) : 'до ' + SF.fmtDate(st.until)) : null,
        iss.length ? h('span', { class: 'badge ' + (err ? 'bad' : 'warn'), title: iss.map(function (x) { return SF.say(x, self.schema); }).join('\n') }, err ? '!' : '?') : null));
      tr.append(h('td', { class: 'sf-rowacts' },
        h('button', { class: 'x', type: 'button', title: groups ? 'Новая ставка с даты (копия строки)' : 'Копия строки', onclick: function (e) { e.stopPropagation(); self.copyRow(t, i, !!groups); } }, '⧉'),
        h('button', { class: 'x del', type: 'button', title: 'Удалить строку', onclick: function (e) { e.stopPropagation(); self.deleteRow(t, i); } }, '×')));
      tbody.append(tr);
    });
    if (!rows.length) tbody.append(h('tr', {}, h('td', { colspan: vc.shown.length + 2, class: 'mut' }, 'Строк нет.')));
    if (primary && this.tab.id === 'books') this.renderUnconfigured(tbody, vc.shown.length + 2);
    var thead = h('thead', {}, h('tr', {}, vc.shown.map(function (c) {
      return h('th', { title: c.description || null, class: c.kind === 'amount' || c.kind === 'number' || c.kind === 'integer' ? 'n' : null }, c.title, c.required ? h('span', { class: 'req' }, ' *') : null);
    }), h('th', {}), h('th', {})));
    return h('div', { class: 'sf-table' },
      h('div', { class: 'sf-th' }, h('h4', {}, t.title), h('span', { class: 'mut3' }, rows.length + ' ' + plural(rows.length, 'строка', 'строки', 'строк')),
        hidden > 0 ? h('span', { class: 'mut3', title: 'Пустые во всех строках поля скрыты; они есть в карточке строки' }, '· ещё полей в карточке: ' + hidden) : null,
        h('span', { class: 'sp' }),
        h('button', { class: 'ibtn', type: 'button', onclick: function () { self.addRow(t); } }, '+ Добавить')),
      h('div', { class: 'k-tbl-wrap' }, h('table', { class: 'k-tbl sf-t' }, thead, tbody)));
  };
  function plural(n, a, b, c) { var m = n % 10, mm = n % 100; return m === 1 && mm !== 11 ? a : m >= 2 && m <= 4 && (mm < 12 || mm > 14) ? b : c; }
  SF.plural = plural;

  F.renderUnconfigured = function (tbody, span) {
    var self = this, recent = this.o.recent ? this.o.recent() : [], miss = SF.unconfigured(this.data, recent);
    if (!miss.length) return;
    tbody.append(h('tr', { class: 'grp' }, h('td', { colspan: span }, 'Есть в позициях, не настроено — ' + miss.length,
      h('span', { class: 'mut3' }, ' · книги последних позиций, которых нет в этом файле (ни по коду книги, ни по кодам отчётов)'),
      miss.length > 1 ? h('button', { class: 'lnk', type: 'button', style: 'margin-left:10px', onclick: function () { miss.forEach(function (r) { self.data.books.push(SF.bookPrefill(r)); }); self.changed(); self.render(); } }, 'добавить все') : null)));
    miss.forEach(function (r) {
      tbody.append(h('tr', { class: 'unconf' },
        h('td', { colspan: span - 2 }, h('span', { class: 'mono' }, r.book_id), ' ', h('span', { class: 'badge warn' }, 'не настроено'),
          h('span', { class: 'mut' }, ' позиции на ' + SF.fmtDate(r.last_date) + (r.positions != null ? ', ' + r.positions + ' ' + plural(r.positions, 'строка', 'строки', 'строк') : '') +
            (r.entity ? ' · юрлицо из отчёта: ' + r.entity : '') + (r.book_type ? ' · сейчас считается как ' + r.book_type : ''))),
        h('td', { colspan: 2, class: 'sf-rowacts' }, h('button', { class: 'ibtn', type: 'button', onclick: function () {
          self.data.books = self.data.books || []; self.data.books.push(SF.bookPrefill(r)); self.changed(); self.render();
          self.editRow(self.L.tables[0], self.data.books.length - 1);
        } }, '+ Добавить'))));
    });
  };

  F.renderFundingSummary = function () {
    var rows = this.data.rules;
    if (!Array.isArray(rows) || !rows.length) return null;
    var item = this.schema.properties.rules.items, bt = item.properties.book_type && item.properties.book_type.enum;
    var M = SF.fundingMatrix(rows, today(), bt || []);
    if (!M.cols.length && !M.special.length) return null;
    var t = h('table', { class: 'k-tbl sf-matrix' }, h('thead', {}, h('tr', {}, h('th', {}, 'Тип книги \\ валюта'), M.cols.map(function (c) { return h('th', {}, c === '*' ? 'любая' : c); }))));
    var tb = h('tbody');
    M.rows.forEach(function (r) {
      tb.append(h('tr', {}, h('td', {}, r === '*' ? 'любой' : h('span', { class: 'tagc' }, r)), M.cols.map(function (c) {
        var x = M.cell(r, c);
        return h('td', { class: x ? '' : 'mut3', title: x ? (SF.BASIS[x.basis] || x.basis || '') + (x.history > 1 ? ' · версий ставки: ' + x.history : '') : null },
          x ? [h('b', { class: 'mono' }, x.text), x.basis ? h('div', { class: 'mut3 small' }, SF.BASIS[x.basis] || x.basis) : null] : '—');
      })));
    });
    t.append(tb);
    return h('div', { class: 'sf-summary' }, h('div', { class: 'sf-th' }, h('h4', {}, 'Действует сегодня'), h('span', { class: 'mut3' }, 'правила по типу книги и валюте; частные (юрлицо, книга, бумага) — в таблице ниже' + (M.special.length ? ': ' + M.special.length : ''))),
      h('div', { class: 'k-tbl-wrap' }, t));
  };

  F.renderFields = function (cols, obj, onChange, base) {
    var self = this, grid = h('div', { class: 'sf-grid' });
    cols.forEach(function (c) {
      if (c.kind === 'object' || c.kind === 'table') return;
      var path = (base || []).concat([c.key]), key = SF.pathKey(path), errs = h('div', { class: 'errs' }), cell;
      var showErrors = function () {  // ошибки поля — сразу под ним (у полей группы; в карточке строки — когда ввод затих)
        var iss = self.byPath[key] || [];
        cell.className = 'sf-c' + (iss.length ? ' bad' : '');
        errs.textContent = '';
        iss.forEach(function (i) { errs.append(h('div', { class: 'err' }, SF.say(i, self.schema))); });
      };
      var ctl = SF.control(c, obj[c.key], function (v) {
        if (v === undefined) delete obj[c.key]; else obj[c.key] = v;
        onChange(); showErrors();
      }, self.o.lists ? self.o.lists() : {}, self.tab.id);
      cell = h('div', {}, ctl, c.description ? h('div', { class: 'hint' }, c.description) : null, errs);
      showErrors();
      grid.append(h('label', { class: 'sf-l', for: ctl.id || ctl.dataset.input || null, title: c.key }, c.title, c.required ? h('span', { class: 'req' }, ' *') : null), cell);
    });
    return grid;
  };
  F.renderObject = function (o) {
    // группа полей (объект верхнего уровня): пустой объект из файла убирается, а не пишется «{}»
    var self = this, obj = isObj(this.data[o.key]) ? this.data[o.key] : {}, wrap = h('div', { class: 'sf-obj' });
    var onChange = function () {
      if (Object.keys(obj).length) self.data[o.key] = obj; else delete self.data[o.key];
      self.changed(); self.refreshHead();
    };
    var cols = SF.columns(o.node), nested = function (c) { return c.kind === 'object' && c.node.properties; };
    wrap.append(h('div', { class: 'sf-th' }, h('h4', {}, o.title), o.node.description ? h('span', { class: 'mut3' }, o.node.description) : null));
    wrap.append(this.renderFields(cols.filter(function (c) { return !nested(c); }), obj, onChange, [o.key]));
    cols.filter(nested).forEach(function (c) {
      var sub = isObj(obj[c.key]) ? obj[c.key] : {};
      var subChange = function () { if (Object.keys(sub).length) obj[c.key] = sub; else delete obj[c.key]; onChange(); };
      wrap.append(h('div', { class: 'sf-sub' }, h('div', { class: 'sf-th' }, h('h5', {}, c.title)), self.renderFields(SF.columns(c.node), sub, subChange, [o.key, c.key])));
    });
    return wrap;
  };

  /* ---------- строка: выезжающая панель ---------- */
  F.drawer = function (title, sub, body, foot) {
    var scrim = h('div', { class: 'k-scrim' }), dr = h('div', { class: 'k-drawer sf-drawer', role: 'dialog', 'aria-label': title });
    var api = {};
    var esc = function (e) { if (e.key === 'Escape') api.close(); };
    api.close = function () { scrim.remove(); dr.remove(); document.removeEventListener('keydown', esc); if (api.onclose) api.onclose(); };
    scrim.addEventListener('click', function () { api.close(); });
    document.addEventListener('keydown', esc);
    dr.append(h('div', { class: 'hd' }, h('div', {}, h('div', { class: 't' }, title), sub ? h('div', { class: 'st' }, sub) : null), h('span', { class: 'sp' }),
      h('button', { class: 'close', type: 'button', 'aria-label': 'Закрыть', onclick: function () { api.close(); } }, '×')), h('div', { class: 'bd' }, body), foot ? h('div', { class: 'ft' }, foot) : null);
    document.body.append(scrim, dr);
    api.el = dr;
    return api;
  };
  // карточка строки: правится копия; «Готово» (или закрытие с изменениями) кладёт её в таблицу, «Отмена» — нет
  F.editRow = function (t, i) {
    var self = this, rows = this.data[t.key], before = rows[i] || {}, row = SF.clone(before), cols = SF.columns(t.item);
    var isNew = !Object.keys(before).length, body = h('div'), timer = null, kind = row.rate_kind;
    var withRow = function () {  // весь файл с правленой строкой — для проверки полей карточки
      var full = {};
      Object.keys(self.data).forEach(function (k) { full[k] = self.data[k]; });
      full[t.key] = rows.map(function (r, j) { return j === i ? row : r; });
      return full;
    };
    var draw = function () {
      body.textContent = '';
      self.issues = SF.validate(self.schema, withRow());
      self.byPath = {};
      self.issues.forEach(function (x) { var k = SF.pathKey(x.path); (self.byPath[k] = self.byPath[k] || []).push(x); });
      var skip = row.rate_kind === 'fixed' ? { index_code: 1, spread_bp: 1 } : row.rate_kind === 'index_spread' ? { fixed_rate_pct: 1 }
        : row.rate_kind === 'none' || row.rate_kind === 'contract' ? { index_code: 1, spread_bp: 1, fixed_rate_pct: 1 } : {};
      body.append(self.renderFields(cols.filter(function (c) { return !skip[c.key] || row[c.key] !== undefined; }), row, later, [t.key, i]));
      var fromAdapter = t === self.L.tables[0] ? self.adapterIssues().filter(function (x) { return x.row === i; }) : [];
      if (fromAdapter.length) body.append(h('div', { class: 'note warn', style: 'margin-top:10px' }, h('b', {}, 'Последняя проверка адаптером:'),
        h('ul', { class: 'sf-issues' }, fromAdapter.map(function (x) { return self.issueLi(x); }))));
    };
    // ошибки под полями обновляются, когда ввод затих (фокус и курсор остаются), сразу — при смене вида ставки
    function later() {
      clearTimeout(timer);
      if (row.rate_kind !== kind) { kind = row.rate_kind; draw(); return; }
      timer = setTimeout(function () {
        var a = document.activeElement, id = a && a.id, s = a && a.selectionStart;
        if (!d.el.isConnected) return;
        draw();
        var b = id && document.getElementById(id);
        if (b) { b.focus(); try { if (s != null) b.setSelectionRange(s, s); } catch (e) { /* select, date */ } }
      }, 700);
    }
    var keyCol = cols.length && cols[0].required ? cols[0].key : null;
    var done = false;
    var apply = function () { done = true; rows[i] = row; d.close(); };
    var foot = h('div', { class: 'row' },
      h('button', { class: 'btn', type: 'button', onclick: apply }, 'Готово'),
      h('button', { class: 'ibtn', type: 'button', onclick: function () { done = true; if (isNew) rows.splice(i, 1); d.close(); } }, 'Отмена'),
      h('span', { class: 'sp' }),
      h('button', { class: 'ibtn danger', type: 'button', onclick: function () { done = true; d.close(); self.deleteRow(t, i); } }, 'Удалить строку'));
    var d = this.drawer((t.title || 'Строка') + (keyCol && row[keyCol] ? ': ' + row[keyCol] : isNew ? ': новая' : ''),
      self.file + ' · строка ' + (i + 1) + ' · в файл — после «Проверить и сохранить»', body, foot);
    d.onclose = function () {
      clearTimeout(timer);
      if (!done) {  // крестик, Esc, щелчок мимо: изменения не теряются; пустая новая строка — убирается
        if (SF.canon(row) !== SF.canon(before)) rows[i] = row;
        else if (isNew) rows.splice(i, 1);
      }
      self.changed(); self.render();
    };
    draw();
    var first = d.el.querySelector('.bd input:not([type=checkbox]), .bd select');
    if (first) first.focus();
  };
  F.addRow = function (t, prefill) {
    this.data[t.key] = this.data[t.key] || [];
    this.data[t.key].push(SF.newRow(t.item, prefill));
    this.render();
    this.editRow(t, this.data[t.key].length - 1);
  };
  F.copyRow = function (t, i, dated) {
    var r = SF.clone(this.data[t.key][i]);
    if (dated) r.valid_from = today();
    this.data[t.key].splice(i + 1, 0, r);
    this.changed(); this.render();
    this.editRow(t, i + 1);
  };
  F.deleteRow = function (t, i) {
    var r = this.data[t.key][i], cols = SF.columns(t.item), key = isObj(r) && cols.length ? r[cols[0].key] : null;
    if (root.confirm && !root.confirm('Удалить строку ' + (i + 1) + (key ? ' (' + key + ')' : '') + '? Файл изменится только после «Проверить и сохранить».')) return;
    this.data[t.key].splice(i, 1);
    this.changed(); this.render();
  };
  F.revert = function () { this.data = SF.clone(this.orig); this.expert = this.expert && !!this.parseError; this.rawError = null; this.changed(); this.render(); };

  /* ---------- «Эксперт»: сырой JSON ---------- */
  F.setExpert = function (on) {
    if (!on && this.rawError) { this.render(); if (this.o.showIssue) this.o.showIssue('Сначала исправьте JSON: ' + this.rawError); return; }
    this.expert = on;
    if (on) { this.rawText = SF.text(this.data); this.rawError = null; }
    this.render();
  };
  F.renderRaw = function () {
    var self = this, msg = h('div', { class: this.rawError ? 'err' : 'mut3' }, this.rawError || 'Изменения JSON сразу видны в форме (снимите «Эксперт»). Сохраняется через ту же проверку.');
    var ta = h('textarea', { class: 'ta', spellcheck: 'false', 'aria-label': this.file });
    ta.value = this.rawText;
    ta.addEventListener('input', function () {
      self.rawText = ta.value;
      try { var d = JSON.parse(ta.value); if (!isObj(d)) throw new Error('нужен JSON-объект'); self.data = d; self.rawError = null; self.changed(); }
      catch (e) { self.rawError = 'Ошибка JSON: ' + e.message; }
      msg.className = self.rawError ? 'err' : 'mut3';
      msg.textContent = self.rawError || (self.errors().length ? 'По схеме: ' + self.errors().map(function (i) { return SF.say(i, self.schema); }).slice(0, 3).join('; ') : 'JSON в порядке.');
      self.refreshHead();
    });
    return h('div', { class: 'sf-raw' }, ta, msg,
      h('div', { class: 'row', style: 'margin-top:6px' }, h('button', { class: 'ibtn', type: 'button', onclick: function () {
        if (self.rawError) return; ta.value = self.rawText = SF.text(self.data);
      } }, 'Форматировать')));
  };

  /* ---------- проверка на сервере, предпросмотр, сохранение ---------- */
  F.review = function () {
    var self = this;
    if (this.expert && this.rawError) { if (this.o.showIssue) this.o.showIssue(this.rawError); return; }
    var body = h('div', {}, h('p', { class: 'mut' }, 'Проверяю: схема формы, затем адаптер читает файл в копии папки настроек…'));
    var foot = h('div', { class: 'row' });
    var d = this.drawer('Проверка и сохранение', this.file, body, foot);
    d.el.classList.add('wide');
    this.o.api('POST', this.url('settings/' + this.tab.id + '/check'), { data: this.data }).then(function (r) {
      self.check = r; self.render();
      self.renderReview(body, foot, r, d);
    }, function (e) { body.textContent = ''; body.append(h('div', { class: 'note err' }, 'Проверка не удалась: ' + e.message)); });
  };
  F.renderReview = function (body, foot, r, d) {
    var self = this, schemaIss = r.schema || [], mine = (r.adapter || []).filter(function (i) { return SF.issueFile(i) === self.file || SF.issueFile(i) === null; });
    var others = (r.adapter || []).filter(function (i) { return mine.indexOf(i) < 0; });
    var newErr = mine.concat(others).filter(function (i) { return i.severity === 'error' && i.new; });
    var schemaErr = schemaIss.filter(function (i) { return i.severity === 'error'; });
    body.textContent = ''; foot.textContent = '';
    var verdict = schemaErr.length ? ['bad', 'Есть ошибки формы — файл не будет сохранён, пока их не исправить.']
      : newErr.length ? ['bad', 'Адаптер не примет этот файл: справочник не обновится, монитор останется на прежних настройках.']
      : !r.adapter_checked ? ['warn', 'Форма в порядке; адаптером не проверено' + (r.adapter_error ? ': ' + r.adapter_error : '') + '.']
      : ['ok', 'Форма в порядке, адаптер принимает файл.'];
    body.append(h('div', { class: 'verdict ' + verdict[0] }, verdict[1]));
    var row = function (i) {
      var li = self.issueLi(i);
      if (i.row != null) li.append(h('span', { class: 'mut3' }, ' · строка ' + (i.row + 1)));
      if (i.path && typeof i.path[1] === 'number') li.append(h('span', { class: 'mut3' }, ' · строка ' + (i.path[1] + 1)));
      if (i.origin) li.append(h('span', { class: 'mut3' }, ' · ' + (i.origin === 'adapter' ? 'адаптер' : i.origin === 'backend' ? 'контракт' : 'форма')));
      return li;
    };
    if (schemaIss.length) body.append(h('h4', {}, 'Форма'), h('ul', { class: 'sf-issues' }, schemaIss.map(row)));
    var fresh = mine.filter(function (i) { return i.new; }), old = mine.filter(function (i) { return !i.new; });
    if (fresh.length) body.append(h('h4', {}, 'Адаптер: новое после изменения'), h('ul', { class: 'sf-issues' }, fresh.map(row)));
    var otherNew = others.filter(function (i) { return i.new; });
    if (otherNew.length) body.append(h('h4', {}, 'Адаптер: в других файлах из-за этого изменения'), h('ul', { class: 'sf-issues' }, otherNew.map(row)));
    if (old.length || others.length - otherNew.length) {
      body.append(h('details', {}, h('summary', {}, 'Было и раньше: ' + (old.length + others.length - otherNew.length)),
        h('ul', { class: 'sf-issues' }, old.concat(others.filter(function (i) { return !i.new; })).map(row))));
    }
    var lines = SF.diff((r.current && r.current.text) || '', r.text), st = SF.diffStats(lines);
    body.append(h('h4', {}, 'Изменения в файле', h('span', { class: 'mut3' }, ' +' + st.added + ' −' + st.removed + (r.current && r.current.exists ? '' : ' · новый файл'))));
    body.append(SF.diffView(lines));
    var force = h('input', { type: 'checkbox' });
    var save = h('button', { class: 'btn', type: 'button', disabled: !!schemaErr.length || (!st.added && !st.removed && r.current && r.current.exists) || !!newErr.length, onclick: function () {
      save.disabled = true; save.textContent = 'Сохраняю…';
      self.save(r).then(function (res) { d.close(); if (self.o.onSaved) self.o.onSaved(self, res); },
        function (e) { save.disabled = false; save.textContent = 'Сохранить'; body.prepend(h('div', { class: 'note err' }, e.status === 409 ? 'Файл изменили, пока вы его правили (другая вкладка или вручную). Закройте окно, нажмите «Отменить» — форма перечитает файл, — и повторите правку.' : 'Не сохранено: ' + e.message)); });
    } }, 'Сохранить');
    foot.append(save);
    if (newErr.length && !schemaErr.length) {
      force.addEventListener('change', function () { save.disabled = !force.checked; });
      foot.append(h('label', { class: 'chk' }, force, 'сохранить всё равно (адаптер будет отвергать файл)'));
    }
    foot.append(h('span', { class: 'sp' }), h('span', { class: 'mut3' }, 'Сохраняется как ' + (r.current && r.current.path || self.file)));
  };
  SF.diffView = function (lines) {
    var hk = SF.hunks(lines, 3), pre = h('div', { class: 'diff mono' });
    if (!hk.some(function (x) { return x.op === '+' || x.op === '-'; })) { pre.append(h('div', { class: 'mut' }, 'Без изменений.')); return pre; }
    hk.forEach(function (x) {
      if (x.op === '…') pre.append(h('div', { class: 'd-skip' }, '… без изменений: ' + x.skipped + ' ' + plural(x.skipped, 'строка', 'строки', 'строк')));
      else pre.append(h('div', { class: 'd-' + (x.op === '+' ? 'add' : x.op === '-' ? 'del' : 'eq') }, h('span', { class: 'd-n' }, x.op === '+' ? '' : x.a), h('span', { class: 'd-n' }, x.op === '-' ? '' : x.b), h('span', { class: 'd-o' }, x.op), x.text));
    });
    return pre;
  };
  F.save = function () {
    var self = this;
    return this.o.api('PUT', this.url('settings/' + this.tab.id), { data: this.data, base_sha256: this.exists ? this.sha : null }).then(function (res) {
      self.lastSave = res;
      return self.load().then(function () { return res; });
    });
  };

  /* ---------- история правок ---------- */
  F.history = function () {
    var self = this, body = h('div', {}, h('p', { class: 'mut' }, 'Загрузка…'));
    var d = this.drawer('История: ' + (this.tab.title || this.file), this.file + ' · кто и когда менял файл', body);
    this.o.api('GET', this.url('config/' + encodeURIComponent(this.file) + '/history')).then(function (r) {
      body.textContent = '';
      if (!r.available) { body.append(h('div', { class: 'note warn' }, 'История недоступна: ' + (r.error || 'хранилище не отвечает') + '. Файл сохраняется и без неё.')); return; }
      if (r.changed_outside) body.append(h('div', { class: 'note warn' }, 'Файл менялся вне программы после последней правки здесь — при следующем сохранении его нынешний вид тоже попадёт в историю.'));
      if (!r.revisions.length) { body.append(h('p', { class: 'mut' }, 'Правок через эту страницу ещё не было.')); return; }
      var tb = h('tbody');
      r.revisions.forEach(function (v) {
        tb.append(h('tr', {},
          h('td', { class: 'mono' }, '№' + v.rev_id), h('td', {}, when(v.changed_at)),
          h('td', {}, v.changed_by === 'file' ? 'вручную в файле' : v.changed_by === 'local' ? 'этот компьютер' : v.changed_by),
          h('td', {}, SF.VIA[v.via] || v.via), h('td', { class: 'n mono' }, Math.max(1, Math.round(v.bytes / 1024)) + ' КБ'),
          h('td', {}, v.current ? h('span', { class: 'badge ok' }, 'сейчас') : null),
          h('td', { class: 'sf-rowacts' },
            h('button', { class: 'ibtn', type: 'button', onclick: function () { self.showRevision(v, body, 'diff'); } }, 'Сравнить'),
            v.current ? null : h('button', { class: 'ibtn', type: 'button', onclick: function () { self.showRevision(v, body, 'load', d); } }, 'В форму'))));
      });
      body.append(h('div', { class: 'k-tbl-wrap' }, h('table', { class: 'k-tbl' }, h('thead', {}, h('tr', {}, h('th', {}, 'Правка'), h('th', {}, 'Когда'), h('th', {}, 'Кто'), h('th', {}, 'Как'), h('th', { class: 'n' }, 'Размер'), h('th', {}), h('th', {}))), tb)),
        h('p', { class: 'hint' }, '«Кто» — «этот компьютер», пока в программе нет пользователей. «В форму» загружает старый вид в форму; файл изменится, только когда вы его сохраните.'));
    }, function (e) { body.textContent = ''; body.append(h('div', { class: 'note err' }, e.message)); });
  };
  F.showRevision = function (v, body, mode, d) {
    var self = this;
    this.o.api('GET', this.url('config/' + encodeURIComponent(this.file) + '/history/' + v.rev_id)).then(function (r) {
      if (mode === 'load') {
        var data; try { data = JSON.parse(r.text); } catch (e) { if (self.o.showIssue) self.o.showIssue('Эта правка — не JSON: ' + e.message); return; }
        self.data = data; self.expert = false; self.changed(); self.render(); d.close();
        if (self.o.showIssue) self.o.showIssue('Правка №' + v.rev_id + ' загружена в форму. Проверьте и сохраните, чтобы вернуть её.', 'ok');
        return;
      }
      var old = body.querySelector('.rev-diff'); if (old) old.remove();
      body.append(h('div', { class: 'rev-diff' }, h('h4', {}, 'Правка №' + v.rev_id + ' → файл сейчас'), SF.diffView(SF.diff(r.text, self.text || ''))));
    }, function (e) { if (self.o.showIssue) self.o.showIssue(e.message); });
  };

  root.SETTINGS_FORMS = SF;
})(typeof window !== 'undefined' ? window : globalThis);
