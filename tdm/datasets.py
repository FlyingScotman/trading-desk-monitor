"""Наборы входных данных центрального сервиса: единый источник правды для
загрузчика (tdm/loader.py), CSV-шаблонов (contract/csv/) и списка данных к сбору
(docs/DATA_REQUIREMENTS.md, генерируется tools/gen_data_docs.py).

Типы полей: str, num, int, date (YYYY-MM-DD), bool (1/0, true/false, да/нет).
kind:
  ref       — справочник: актуальная версия, upsert по ключу;
  snapshot  — срез на дату: файл заменяет все строки своих дат (и книг, если есть book_id);
  series    — временной ряд: upsert по (дата, ключ).
replace_all — справочник целиком заменяется файлом (строки, которых нет в файле, удаляются).
priority: 1 — обязательно для работы мониторов, 2 — желательно, 3 — потом / если есть.
"""
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class Col:
    name: str
    type: str
    required: bool
    desc: str
    example: str = ""
    default: object = None   # подставляется, если ячейка пуста


@dataclass
class Dataset:
    name: str            # имя файла: <name>.csv или <name>_<что угодно>.csv
    table: str
    kind: str            # ref | snapshot | series
    key: Tuple[str, ...]
    priority: int
    title: str
    purpose: str
    depth: str           # глубина/частота
    sources: str         # где обычно брать
    cols: List[Col] = field(default_factory=list)
    date_col: Optional[str] = None
    replace_by: Tuple[str, ...] = ()   # snapshot: колонки, по которым заменяется срез
    example2: Optional[dict] = None     # вторая строка-пример
    replace_all: bool = False           # ref: файл заменяет справочник целиком


def C(name, type_, required, desc, example="", default=None):
    return Col(name, type_, required, desc, example, default)


DATASETS: List[Dataset] = [
    # ------------------------------------------------------------------ справочники
    Dataset(
        "books", "ref_book", "ref", ("book_id",), 1, "Книги и деск",
        "Фильтры «Деск» и «Книги», группировки, привязка позиций и фондирования.",
        "текущая версия; обновлять при изменениях", "внутренний справочник книг / фронт-система",
        [C("book_id", "str", True, "код книги, стабильный", "OFZ"),
         C("name", "str", True, "название для экрана", "ОФЗ"),
         C("desk", "str", True, "деск (Rates, Credit, FX Credit, FX, …)", "Rates"),
         C("trader", "str", False, "ответственный трейдер", "А. Иванов"),
         C("active", "bool", False, "книга используется (по умолчанию 1)", "1", 1),
         C("ord", "int", False, "порядок на экране", "1", 0),
         C("book_type", "str", False, "*1.3, обязательно:* режим фондирования TRADING / HTM / DERIVS, ровно один на книгу. "
           "Пусто — выводится из позиций прогона (есть облигации → TRADING, иначе DERIVS)", "TRADING"),
         C("entity", "str", False, "*1.3, обязательно:* юрлицо (иерархия юрлицо → деск → книга)", "Головной банк"),
         C("ocp_to", "str", False, "*1.3:* книга деривативов, куда вечером передаётся ОВП (для TRADING с валютными бумагами)", "")]),
    Dataset(
        "funding_rates", "ref_funding_rate", "ref", ("book_type", "desk", "book_id", "entity", "instrument_id", "ccy"), 1,
        "Справочник ставок фондирования (1.3)",
        "Ставки фондирования облигаций торговых книг (строки POS) и остатков ДС книг деривативов (CASH); "
        "состав ставки нужен монитору для сценариев кэрри. Пустое поле — «любое значение»; побеждает самое конкретное "
        "правило: бумага (16) > книга (8) > юрлицо (4) > деск (2) > режим книги (1). Демо-набор (синтетические значения, "
        "не правила организации): TRADING — RUB КС + спред деска, CNY RUSFAR CNY + спред, USD / EUR / CHF фикс. 6 %; "
        "DERIVS — RUB RUONIA, CNY RUSFAR CNY, USD SOFR, EUR €STR, CHF SARON, спред 0.",
        "текущая версия (истории ставок пока нет: прошлые дни пересчитываются по текущей)", "казначейство",
        [C("book_type", "str", False, "TRADING / HTM / DERIVS", "TRADING"),
         C("desk", "str", False, "деск", ""),
         C("book_id", "str", False, "особая ставка книги", ""),
         C("entity", "str", False, "юрлицо (вместе с instrument_id — особый спред юрлица по бумаге)", ""),
         C("instrument_id", "str", False, "бумага: instrument_id, ISIN или имя (индивидуальная корректировка — TODO)", ""),
         C("ccy", "str", True, "валюта бумаги / остатка", "RUB"),
         C("index_name", "str", False, "индекс: КС, RUSFAR CNY, RUONIA, SOFR, €STR, SARON", "КС"),
         C("spread_bp", "num", False, "спред к индексу, б.п.", "30"),
         C("fixed_rate", "num", False, "фикс. ставка, % (hard ccy); вместо индекса", ""),
         C("note", "str", False, "комментарий", "спред деска")],
        replace_all=True),
    Dataset(
        "issuers", "ref_issuer", "ref", ("issuer_id",), 1, "Эмитенты и группы",
        "Концентрации по группам связанных заёмщиков, секторы, сегмент «Гос», референсы CDS.",
        "текущая версия", "справочник контрагентов / кредитный риск / Cbonds / МосБиржа",
        [C("issuer_id", "str", True, "код эмитента (ИНН, LEI или внутренний)", "7710168360"),
         C("name", "str", True, "краткое название для экрана", "Минфин РФ"),
         C("issuer_group", "str", True, "группа связанных заёмщиков", "Российская Федерация"),
         C("sector", "str", False, "отрасль", "Суверен"),
         C("country", "str", False, "страна риска, ISO-2", "RU"),
         C("is_gov", "bool", False, "суверен РФ", "1", 0),
         C("is_quasi", "bool", False, "квазисуверен (госкомпания, госбанк)", "0", 0)]),
    Dataset(
        "issuer_ratings", "ref_issuer_rating", "series", ("issuer_id", "agency", "date"), 1, "Рейтинги эмитентов (история)",
        "Разрезы по рейтингу, кредитные сценарии по рейтинговым корзинам, исторические спред-шоки по рейтингам.",
        "все изменения рейтинга за год (дата присвоения)", "АКРА / Эксперт РА / НКР / НРА или внутренний композитный рейтинг",
        [C("issuer_id", "str", True, "код эмитента из issuers", "7710168360"),
         C("date", "date", True, "дата, с которой действует рейтинг", "2025-10-13"),
         C("agency", "str", True, "агентство или COMPOSITE (тот, что показывает монитор)", "COMPOSITE"),
         C("rating", "str", True, "рейтинг в национальной шкале без префикса: AAA, AA+, …", "AAA")],
        date_col="date"),
    Dataset(
        "counterparties", "ref_counterparty", "ref", ("counterparty_id",), 2, "Контрагенты",
        "Колонка «Контрагент» по OTC и фондированию, группировки в Pivot Studio.",
        "текущая версия", "справочник контрагентов",
        [C("counterparty_id", "str", True, "код контрагента", "CCP"),
         C("name", "str", True, "название", "ЦК (клиринг)"),
         C("type", "str", False, "ЦК / банк / биржа / казначейство", "ЦК"),
         C("issuer_id", "str", False, "если контрагент — эмитент из issuers", "")]),
    Dataset(
        "instruments", "ref_instrument", "ref", ("instrument_id",), 1, "Облигации (бумаги)",
        "Карточка позиции, разрезы по классу актива/сроку/купону, расчёт сроков, флоатеры, секьюритизация.",
        "все бумаги, которые были в позиции за год (включая погашенные)", "Cbonds / МосБиржа ISS / внутренний справочник ЦБ",
        [C("instrument_id", "str", True, "код бумаги (ISIN, если есть)", "SU26242RMFS0"),
         C("isin", "str", False, "ISIN", "SU26242RMFS0"),
         C("name", "str", True, "название для экрана", "ОФЗ 26242"),
         C("issuer_id", "str", True, "эмитент из issuers", "7710168360"),
         C("asset_class", "str", True, "ОФЗ-ПД, ОФЗ-ПК, Корп RUB фикс, Корп RUB флоатер, Корп валютные, Суверен валютный, Секьюритизация", "ОФЗ-ПД"),
         C("segment", "str", True, "Гос / Корп / Секьюритизация", "Гос"),
         C("ccy", "str", True, "валюта номинала: RUB, USD, CNY, EUR, CHF", "RUB"),
         C("face_value", "num", True, "номинал одной бумаги, в валюте", "1000"),
         C("issue_date", "date", False, "дата размещения", "2019-05-15"),
         C("maturity", "date", True, "дата погашения", "2029-08-29"),
         C("coupon_type", "str", True, "fix / float", "fix"),
         C("coupon_rate", "num", False, "фикс. купон, % годовых", "9.0"),
         C("coupon_freq", "int", False, "купонов в год", "2"),
         C("day_count", "str", False, "база: ACT/365, 30/360, …", "ACT/365"),
         C("index_name", "str", False, "индекс флоатера: КС, RUONIA, …", ""),
         C("margin", "num", False, "спред к индексу флоатера, %", ""),
         C("amortizing", "bool", False, "амортизация номинала", "0"),
         C("offer_date", "date", False, "ближайшая оферта (put/call): к ней считаются доходность, спред и срок в таблице облигаций и в истории выпуска", ""),
         C("is_mbs", "bool", False, "ИЦБ / ABS", "0"),
         C("collateral_eligible", "bool", False, "входит в ломбардный список / РЕПО с ЦК", "1"),
         C("call_date", "date", False, "*1.3:* ближайший колл (бессрочные, субординированные) — для JTD и риска продления", ""),
         C("is_subord", "bool", False, "*1.3:* субординированный выпуск", "0")]),
    Dataset(
        "bond_cashflows", "ref_cashflow", "ref", ("instrument_id", "date", "type"), 2, "График платежей облигаций",
        "Лестница денежных потоков, ближайшие купоны/погашения/оферты; нужен расчётному сервису для переоценки.",
        "будущие платежи по всем бумагам в позиции", "МосБиржа ISS (bondization) / Cbonds",
        [C("instrument_id", "str", True, "бумага из instruments", "SU26242RMFS0"),
         C("date", "date", True, "дата платежа", "2027-03-03"),
         C("type", "str", True, "coupon / amort / redemption / offer", "coupon"),
         C("rate", "num", False, "ставка купона, % (если известна)", "9.0"),
         C("amount", "num", False, "сумма на 1 бумагу, в валюте", "44.88")]),
    Dataset(
        "otc_deals", "ref_otc_deal", "ref", ("deal_id",), 1, "Условия OTC-сделок (IRS, XCCY, CDS, FX)",
        "Карточка сделки, сторона, ноги, индекс, контрагент; основа для расчёта рисков деривативов.",
        "все OTC-сделки, живые хотя бы один день за год", "фронт-система / бэк-офис",
        [C("deal_id", "str", True, "номер сделки, стабильный", "IRS-000123"),
         C("instr_type", "str", True, "IRS / XCCY / CDS / FX", "IRS"),
         C("book_id", "str", True, "книга", "RATES"),
         C("trade_date", "date", True, "дата заключения", "2026-04-01"),
         C("start_date", "date", False, "начало", "2026-04-03"),
         C("maturity", "date", True, "окончание / value date", "2031-04-03"),
         C("ccy", "str", True, "валюта номинала (для XCCY и FX — иностранная)", "RUB"),
         C("notional", "num", True, "номинал в ccy (FX — сумма со знаком)", "3500000000"),
         C("side", "str", True, "IRS: PAY/REC фикс; XCCY: PAY/RECFX валюту; CDS: BUY/SELL защиту; FX: BUY/SELL", "PAY"),
         C("fixed_rate", "num", False, "фикс. ставка, % (IRS; рублёвая нога XCCY)", "12.70"),
         C("float_index", "str", False, "плавающий индекс (IRS)", "RUONIA"),
         C("float_spread_bp", "num", False, "спред к индексу, б.п.", "0"),
         C("notional_rub", "num", False, "XCCY: рублёвый номинал", ""),
         C("reference_issuer_id", "str", False, "CDS: референс", ""),
         C("cds_spread_bp", "num", False, "CDS: контрактный спред, б.п.", ""),
         C("fx_rate", "num", False, "FX: курс сделки", ""),
         C("counterparty_id", "str", False, "контрагент", "CCP"),
         C("cleared", "bool", False, "через ЦК", "1"),
         C("purpose", "str", False, "*1.3:* OCP_TRANSFER — вечерняя передача ОВП из книги облигаций в книгу деривативов "
           "(fx_rate — курс ЦБ на завтра, maturity — T+1)", "")]),
    Dataset(
        "curves", "ref_curve", "ref", ("curve_id",), 1, "Определения кривых",
        "Какие кривые есть, их валюта и тип; ключи кривых используются в рисках и сценариях.",
        "один раз", "методология расчётного сервиса",
        [C("curve_id", "str", True, "ключ: RUB OFZ, RUB RUONIA, RUB KEY, USD SOFR, CNY, EUR ESTR, CHF SARON", "RUB OFZ"),
         C("name", "str", True, "подпись", "ОФЗ (КБД)"),
         C("ccy", "str", True, "валюта", "RUB"),
         C("kind", "str", True, "bond / swap", "bond"),
         C("index_name", "str", False, "индекс, который кривая проецирует", ""),
         C("source", "str", False, "откуда берутся точки", "МосБиржа КБД"),
         C("ord", "int", False, "порядок на экране", "1", 0)]),
    Dataset(
        "calendar", "ref_calendar", "ref", ("date",), 3, "Рабочие дни",
        "Окна исторических сценариев (1Д/10Д в рабочих днях), «вчера» для изменений.",
        "год назад — год вперёд", "производственный календарь РФ / МосБиржа",
        [C("date", "date", True, "дата", "2026-10-13"),
         C("is_business_day", "bool", True, "рабочий день рынка", "1")]),
    Dataset(
        "cbr_meetings", "ref_cbr_meeting", "ref", ("date",), 1, "Заседания ЦБ по ключевой ставке (1.3)",
        "Траектории КС в сценариях кэрри и фондирования (docs/SCENARIOS.md §7).",
        "опубликованный график на год вперёд", "ЦБ",
        [C("date", "date", True, "дата решения", "2026-10-23"),
         C("effective_date", "date", False, "с какого дня действует новая КС (пусто — следующий рабочий день)", "2026-10-26")]),

    # ------------------------------------------------------------------ ежедневные срезы
    Dataset(
        "positions", "pos_snapshot", "snapshot", ("as_of", "position_id"), 1, "Позиции (ежедневный снэпшот)",
        "Основа всего: что лежит в каждой книге на конец дня. Расчётный сервис считает по ним риски; монитор — новые сделки, историю объёмов.",
        "каждый рабочий день за год, конец дня", "фронт-система / бэк-офис (ваши ежедневные снэпшоты)",
        [C("as_of", "date", True, "дата среза", "2026-10-13"),
         C("book_id", "str", True, "книга", "OFZ"),
         C("position_id", "str", True, "id позиции, стабильный между днями (бумага: book+ISIN; OTC: deal_id)", "OFZ|SU26242RMFS0"),
         C("instrument_id", "str", False, "бумага из instruments (для облигаций)", "SU26242RMFS0"),
         C("deal_id", "str", False, "сделка из otc_deals (для OTC)", ""),
         C("quantity", "num", False, "количество бумаг, со знаком (шорт < 0)", "4200000"),
         C("face_amount", "num", False, "номинал в валюте, со знаком (если нет quantity)", "4200000000"),
         C("avg_price", "num", False, "средняя цена покупки, % от номинала", "88.10"),
         C("book_value", "num", False, "балансовая стоимость, RUB", ""),
         C("mark_price", "num", False, "цена переоценки источника, % (для сверки)", "87.60"),
         C("accrued", "num", False, "НКД, в валюте", ""),
         C("mv_rub", "num", False, "MV по данным источника, RUB (для сверки)", "3680000000"),
         C("purchase_date", "date", False, "*1.3, книги HTM:* дата покупки", ""),
         C("purchase_price", "num", False, "*1.3, HTM:* чистая цена покупки, % номинала (база фондирования)", ""),
         C("purchase_yield", "num", False, "*1.3, HTM:* доходность покупки, % (начисление)", ""),
         C("contract_rate", "num", False, "*1.3, HTM:* контрактная ставка фондирования, % (до погашения)", "")],
        date_col="as_of", replace_by=("as_of", "book_id")),
    Dataset(
        "funding", "fund_snapshot", "snapshot", ("as_of", "deal_id"), 1, "Фондирование (ежедневный снэпшот)",
        "С 1.3 — только сделки, которые не выводятся из позиций и остатков: РЕПО деска РЕПО против книги (покрытие шорта), "
        "особые сделки. Фондирование облигаций торговых книг строится из positions + funding_rates, книг деривативов — из cash_balances.",
        "каждый рабочий день за год", "казначейство (FTP), бэк-офис (РЕПО, FX-свопы)",
        [C("as_of", "date", True, "дата среза", "2026-10-13"),
         C("deal_id", "str", True, "номер сделки", "REPO-88812"),
         C("book_id", "str", True, "книга, которую фондирует", "OFZ"),
         C("type", "str", True, "РЕПО с ЦК, TSY FTP, Обратное РЕПО, FX-своп, Гарантийное обеспечение", "РЕПО с ЦК"),
         C("ccy", "str", True, "валюта", "RUB"),
         C("amount", "num", True, "сумма в валюте; < 0 привлечение, > 0 размещение", "-14500000000"),
         C("rate_type", "str", False, "fix / float", "float"),
         C("index_name", "str", False, "индекс плавающей ставки", "RUSFAR O/N"),
         C("spread_bp", "num", False, "спред к индексу, б.п.", "0"),
         C("rate", "num", True, "эффективная ставка на дату, %", "14.28"),
         C("start_date", "date", False, "начало", "2026-10-13"),
         C("maturity", "date", True, "окончание", "2026-10-14"),
         C("counterparty_id", "str", False, "контрагент", "CCP"),
         C("collateral", "str", False, "обеспечение (ISIN или описание)", "ОФЗ-ПД")],
        date_col="as_of", replace_by=("as_of", "book_id")),
    Dataset(
        "cash_balances", "cash_snapshot", "snapshot", ("as_of", "book_id", "ccy"), 1, "Остатки ДС книг деривативов (1.3)",
        "Фондирование книг DERIVS: остаток в каждой валюте × O/N-ставка валюты (строки CASH); FX-переоценка остатков; ОВП.",
        "каждый рабочий день за год", "выгрузка из системы, ежедневно",
        [C("as_of", "date", True, "дата среза", "2026-10-13"),
         C("book_id", "str", True, "книга DERIVS", "RATES"),
         C("ccy", "str", True, "валюта", "RUB"),
         C("balance", "num", True, "остаток в валюте: + размещён, − привлечён", "-1350000000")],
        date_col="as_of", replace_by=("as_of", "book_id")),
    Dataset(
        "trades", "trade_log", "series", ("trade_date", "trade_id"), 2, "Сделки дня",
        "PnL новых сделок (цена сделки → закрытие), подсветка «новые сегодня». Без него новые сделки видны только как появившиеся позиции.",
        "каждый рабочий день за год (выгрузка за день, не real-time)", "фронт-система",
        [C("trade_date", "date", True, "дата сделки", "2026-10-13"),
         C("trade_id", "str", True, "номер сделки", "T-551023"),
         C("book_id", "str", True, "книга", "OFZ"),
         C("position_id", "str", True, "позиция, которую меняет сделка", "OFZ|SU26247RMFS5"),
         C("instrument_id", "str", False, "бумага", "SU26247RMFS5"),
         C("deal_id", "str", False, "OTC-сделка", ""),
         C("side", "str", True, "BUY / SELL", "BUY"),
         C("quantity", "num", False, "количество бумаг", "1500000"),
         C("face_amount", "num", False, "номинал в валюте", ""),
         C("price", "num", False, "цена сделки, % / курс", "92.45"),
         C("counterparty_id", "str", False, "контрагент", "")],
        date_col="trade_date"),
    Dataset(
        "pnl_official", "pnl_official", "series", ("as_of", "book_id", "position_id"), 1, "Официальный PnL",
        "Сверка «официальный vs explained», разрывы (break) по позициям и книгам, история PnL. С 1.3 — управленческий PnL "
        "по бумаге и книге, по курсам ЦБ, с фондированием; приходит на T+1 и прикрепляется к EOD-прогону за ту же дату: "
        "python -m tdm attach-official --as-of <дата>.",
        "каждый рабочий день за год; по позициям, минимум — по книгам", "бэк-офис / управленческий учёт",
        [C("as_of", "date", True, "дата", "2026-10-13"),
         C("book_id", "str", True, "книга", "OFZ"),
         C("position_id", "str", False, "позиция; пусто = итог по книге", "OFZ|SU26242RMFS0"),
         C("pnl_rub", "num", True, "PnL за день, RUB (+ прибыль)", "-6600000"),
         C("comment", "str", False, "комментарий", ""),
         C("carry", "num", False, "*1.3:* компонента: начисление, RUB (временный состав)", ""),
         C("funding", "num", False, "*1.3:* компонента: фондирование, RUB", ""),
         C("fx", "num", False, "*1.3:* компонента: FX по курсам ЦБ, RUB", "")],
        date_col="as_of"),

    # ------------------------------------------------------------------ рынок
    Dataset(
        "md_curves", "md_curve_point", "series", ("date", "curve_id", "tenor_years"), 1, "Кривые (история)",
        "Уровни и изменения кривых, PnL explain по ставкам, исторические сценарии, график кривых.",
        "каждый рабочий день за год; узлы — любые, лучше покрывающие 1М…20Л", "МосБиржа (КБД, кривые RUONIA/КС-свопов), ЦБ, Bloomberg/Refinitiv для USD/EUR/CNY",
        [C("date", "date", True, "дата", "2026-10-13"),
         C("curve_id", "str", True, "кривая из curves", "RUB OFZ"),
         C("tenor_years", "num", True, "срок узла в годах (0.0833 = 1М)", "5"),
         C("rate", "num", True, "ставка, % (нуль-купонная или par — одинаково для всех дат)", "13.10"),
         C("source", "str", False, "источник", "MOEX")],
        date_col="date"),
    Dataset(
        "md_fx", "md_fx", "series", ("date", "ccy"), 1, "Курсы валют (история)",
        "Рыночные курсы на срез ~19:00 (внешние котировки USDRUB, EURRUB, CHFRUB, CNYRUB): флэш-PnL, ОВП, FX-шоки, исторические сценарии. "
        "Официальные курсы ЦБ — отдельный набор md_fx_cbr.",
        "каждый рабочий день за год", "внешние котировки на ~19:00, подаются изнутри; один источник на весь год",
        [C("date", "date", True, "дата", "2026-10-13"),
         C("ccy", "str", True, "USD, CNY, EUR, CHF", "USD"),
         C("rate", "num", True, "RUB за 1 единицу валюты", "81.45"),
         C("source", "str", False, "источник", "внутр. котировки 19:00")],
        date_col="date"),
    Dataset(
        "md_fx_cbr", "md_fx_cbr", "series", ("date", "ccy"), 1, "Официальные курсы ЦБ (1.3)",
        "Официальный PnL и сверка флэша с ним (курс, действующий на дату), курс передачи ОВП (установленный сегодня на завтра).",
        "каждый день за год, по дате действия", "ЦБ",
        [C("date", "date", True, "дата действия курса (курс, установленный в пятницу на субботу–понедельник, — на каждую из этих дат "
           "или на дату начала действия)", "2026-10-13"),
         C("ccy", "str", True, "USD, EUR, CNY, CHF", "USD"),
         C("rate", "num", True, "RUB за 1 единицу", "81.12"),
         C("set_date", "date", False, "дата установления", "2026-10-12")],
        date_col="date"),
    Dataset(
        "md_fixings", "md_fixing", "series", ("date", "index_name"), 1, "Фиксинги индексов (история)",
        "Ставки флоатеров и фондирования (начисление по ставке каждого календарного дня), бенчмарк стоимости фондирования, кэрри свопов. "
        "КС — по дате вступления в силу: значение действует с этой даты до следующей записи.",
        "каждый рабочий день за год", "ЦБ (КС, RUONIA), МосБиржа (RUSFAR, RUSFAR CNY), ФРС / ЕЦБ / SNB (SOFR, €STR, SARON)",
        [C("date", "date", True, "дата (для КС — дата вступления в силу)", "2026-10-13"),
         C("index_name", "str", True, "КС, RUONIA, RUSFAR O/N, RUSFAR 1W, RUSFAR 1M, RUSFAR CNY, SOFR, €STR, SARON", "RUONIA"),
         C("value", "num", True, "значение, %", "14.32")],
        date_col="date"),
    Dataset(
        "md_bond_quotes", "md_bond_quote", "series", ("date", "instrument_id"), 1, "Котировки облигаций (история)",
        "Таблица облигаций в мониторах (изменения цены, доходности и спреда за день) и история выпуска (цена, доходность, спред к выбранной кривой за год); спреды эмитентов (кредитные сценарии, исторические спред-шоки), сверка переоценки.",
        "каждый рабочий день за год, по всем бумагам в позиции (и бенчмаркам эмитентов)", "МосБиржа (цена закрытия/признаваемая), НФА/BVAL, Cbonds",
        [C("date", "date", True, "дата", "2026-10-13"),
         C("instrument_id", "str", True, "бумага из instruments", "RU000A105X12"),
         C("clean_price", "num", True, "чистая цена, % от номинала", "97.85"),
         C("ytm", "num", False, "доходность, %: к ближайшей оферте, пока она впереди (offer_date в instruments), иначе к погашению — та же база, что ytm в расчёте. Без неё нет истории доходности и спреда к кривой", "15.42"),
         C("z_spread_bp", "num", False, "спред к своей кривой оценки, б.п.: Z-спред (фикс), DM к индексу (флоатер) — тот же тип, что spread_type в расчёте", "182"),
         C("g_spread_bp", "num", False, "G-спред к КБД, б.п. (рублёвые бумаги)", "176"),
         C("accrued", "num", False, "НКД, % номинала", "1.23"),
         C("volume_rub", "num", False, "оборот торгов, RUB (оценка ликвидности)", ""),
         C("source", "str", False, "источник цены", "MOEX")],
        date_col="date"),
    Dataset(
        "md_cds", "md_cds_quote", "series", ("date", "issuer_id", "tenor_years"), 3, "Спреды CDS (история)",
        "Переоценка CDS-книги. Если рынка CDS нет — расчётный сервис использует прокси от спредов облигаций.",
        "каждый рабочий день за год, если есть", "внутренние котировки / брокеры",
        [C("date", "date", True, "дата", "2026-10-13"),
         C("issuer_id", "str", True, "референс из issuers", "7736050003"),
         C("tenor_years", "num", True, "срок, лет", "5"),
         C("spread_bp", "num", True, "спред, б.п.", "105")],
        date_col="date"),

    # ------------------------------------------------------------------ история итогов
    Dataset(
        "desk_history", "desk_history", "series", ("as_of",), 2, "Итоги деска по дням (если нет исторических прогонов)",
        "Графики PnL за период и спарклайны DV01/CS01, пока расчётный сервис не пересчитал год назад.",
        "каждый рабочий день за год", "управленческая отчётность / расчётный сервис",
        [C("as_of", "date", True, "дата", "2026-10-12"),
         C("pnl_official", "num", True, "официальный PnL деска за день, RUB", "4200000"),
         C("pnl_explained", "num", True, "объяснённый PnL, RUB (если нет — = official)", "3900000"),
         C("dv01", "num", True, "DV01 деска на конец дня, RUB/б.п.", "-12100000"),
         C("cs01", "num", True, "CS01 деска на конец дня, RUB/б.п.", "-20300000")],
        date_col="as_of"),
]

BY_NAME = {d.name: d for d in DATASETS}
