"""
Russian/Kazakh value transformation functions for Word Constructor.

Provides:
  - числопрописью  — integer to Russian words (with gender)
  - валюта         — monetary amount to words (тенге/рубли/доллары/евро)
  - падежи         — word/phrase declension by grammatical case
  - пол            — gender detection from Russian full name
  - дата           — date formatting (1C-style patterns, Russian + Kazakh)
"""
from __future__ import annotations

import re
from datetime import date as _date
from decimal import Decimal, InvalidOperation
from typing import Optional

# ── optional dependencies ────────────────────────────────────────────────────

try:
    from num2words import num2words as _n2w
    _HAS_N2W = True
except ImportError:
    _HAS_N2W = False

try:
    import pymorphy2 as _pm  # type: ignore
    _morph = _pm.MorphAnalyzer()
    _HAS_PM = True
except ImportError:
    _HAS_PM = False
    _morph = None  # type: ignore

# ── value-type detection ─────────────────────────────────────────────────────

def _clean_num(value: str) -> str:
    return value.strip().replace('\u00a0', '').replace(' ', '').replace(',', '.')


def is_number(value: str) -> bool:
    try:
        Decimal(_clean_num(value))
        return bool(re.search(r'\d', value))
    except InvalidOperation:
        return False


def is_name(value: str) -> bool:
    """2–4 capitalised Cyrillic words → likely a Russian name."""
    words = value.strip().split()
    return (
        2 <= len(words) <= 4
        and all(re.fullmatch(r'[А-ЯЁ][А-ЯЁа-яё\-]*', w) for w in words)
    )


# ── plural helper ─────────────────────────────────────────────────────────────

def _pluralise(n: int, form1: str, form2: str, form5: str) -> str:
    """Return correct Russian plural form for n."""
    n = abs(n) % 100
    if 11 <= n <= 19:
        return form5
    n %= 10
    if n == 1:
        return form1
    if 2 <= n <= 4:
        return form2
    return form5


# ── числопрописью ─────────────────────────────────────────────────────────────

def число_прописью(value: str, gender: str = 'м') -> str:
    """Convert integer part of value to Russian words.
    gender: 'м' masculine, 'ж' feminine, 'с' neuter
    """
    if not _HAS_N2W:
        return value
    try:
        n = int(Decimal(_clean_num(value)))
        gmap = {
            'м': 'masculine', 'm': 'masculine',
            'ж': 'feminine',  'f': 'feminine',
            'с': 'neuter',    'n': 'neuter',
        }
        g = gmap.get(gender.lower(), 'masculine')
        try:
            return _n2w(n, lang='ru', gender=g)
        except TypeError:
            return _n2w(n, lang='ru')
    except Exception:
        return value


# ── валюта прописью ───────────────────────────────────────────────────────────

_CURRENCIES: dict[str, dict] = {
    'KZT': {
        'int_forms':  ('тенге',   'тенге',   'тенге'),    # invariable
        'frac_forms': ('тиын',    'тиына',   'тиын'),
        'int_gender': 'м',
        'frac_gender': 'м',
    },
    'RUB': {
        'int_forms':  ('рубль',   'рубля',   'рублей'),
        'frac_forms': ('копейка', 'копейки', 'копеек'),
        'int_gender': 'м',
        'frac_gender': 'ж',
    },
    'USD': {
        'int_forms':  ('доллар',  'доллара', 'долларов'),
        'frac_forms': ('цент',    'цента',   'центов'),
        'int_gender': 'м',
        'frac_gender': 'м',
    },
    'EUR': {
        'int_forms':  ('евро',    'евро',    'евро'),
        'frac_forms': ('цент',    'цента',   'центов'),
        'int_gender': 'м',
        'frac_gender': 'м',
    },
}


def валюта_прописью(value: str, currency: str = 'KZT') -> str:
    if not _HAS_N2W:
        return value
    try:
        amount = Decimal(_clean_num(value))
        int_part  = int(amount)
        frac_part = int(round((amount - int_part) * 100))

        cfg = _CURRENCIES.get(currency.upper(), _CURRENCIES['KZT'])

        int_words  = число_прописью(str(int_part),  cfg['int_gender'])
        frac_words = число_прописью(str(frac_part), cfg['frac_gender'])

        int_noun  = _pluralise(int_part,  *cfg['int_forms'])
        frac_noun = _pluralise(frac_part, *cfg['frac_forms'])

        return f"{int_words} {int_noun} {frac_words} {frac_noun}"
    except Exception:
        return value


# ── declension helpers ────────────────────────────────────────────────────────

_CASE_MAP = {
    'им':  'nomn', 'nomn': 'nomn',
    'рд':  'gent', 'gent': 'gent', 'родительный': 'gent',
    'дт':  'datv', 'datv': 'datv', 'дательный':   'datv',
    'вн':  'accs', 'accs': 'accs', 'винительный': 'accs',
    'тв':  'ablt', 'ablt': 'ablt', 'творительный':'ablt',
    'пр':  'loct', 'loct': 'loct', 'предложный':  'loct',
}

# Readable labels for each case
CASE_LABELS: dict[str, tuple[str, str]] = {
    'gent': ('Родительный', 'кого? чего?'),
    'datv': ('Дательный',   'кому? чему?'),
    'accs': ('Винительный', 'кого? что?'),
    'ablt': ('Творительный','кем? чем?'),
    'loct': ('Предложный',  'о ком? о чём?'),
}


def _inflect_word(word: str, pycase: str) -> str:
    if not _HAS_PM:
        return word
    parses = _morph.parse(word)
    if not parses:
        return word
    # Prefer name-entity parses (Name/Patr/Surn) over common nouns
    best = parses[0]
    for p in parses:
        tag = str(p.tag)
        if any(t in tag for t in ('Name', 'Patr', 'Surn')):
            best = p
            break
    inflected = best.inflect({pycase})
    if not inflected:
        return word
    result = inflected.word
    # Restore original capitalisation
    if word[0].isupper():
        result = result[0].upper() + result[1:]
    return result


def склонить(text: str, case: str) -> str:
    """Decline every word in text by the given Russian grammatical case."""
    if not _HAS_PM or not text.strip():
        return text
    pycase = _CASE_MAP.get(case.lower(), 'nomn')
    return ' '.join(_inflect_word(w, pycase) for w in text.split())


# ── date formatting (1C-style) ────────────────────────────────────────────────

# Russian month names — genitive case (used in dates: "10 апреля")
_RU_MONTHS_GENT = [
    '', 'января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
    'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря',
]
# Russian month names — nominative (for headers: "Апрель 2026")
_RU_MONTHS_NOM = [
    '', 'январь', 'февраль', 'март', 'апрель', 'май', 'июнь',
    'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь',
]
# Russian month abbreviations (1C МММ)
_RU_MONTHS_SHORT = [
    '', 'янв.', 'февр.', 'мар.', 'апр.', 'мая', 'июн.',
    'июл.', 'авг.', 'сент.', 'окт.', 'нояб.', 'дек.',
]
# Russian weekday names — full / short (Mon=0)
_RU_WEEKDAYS_FULL = ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье']
_RU_WEEKDAYS_SHORT = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']

# Kazakh month names (genitive/nominative — same in Kazakh)
_KK_MONTHS = [
    '', 'қаңтар', 'ақпан', 'наурыз', 'сәуір', 'мамыр', 'маусым',
    'шілде', 'тамыз', 'қыркүйек', 'қазан', 'қараша', 'желтоқсан',
]
# Kazakh weekday names
_KK_WEEKDAYS = ['дүйсенбі', 'сейсенбі', 'сәрсенбі', 'бейсенбі', 'жұма', 'сенбі', 'жексенбі']

# Date parsing patterns: (regex, (year_g, month_g, day_g))
_DATE_PATTERNS = [
    (re.compile(r'^(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})$'), ('y3', 'm2', 'd1')),  # DD.MM.YYYY
    (re.compile(r'^(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})$'), ('y1', 'm2', 'd3')),  # YYYY-MM-DD
    (re.compile(r'^(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2})$'), ('y3', 'm2', 'd1')),  # DD.MM.YY
]


def is_date(value: str) -> bool:
    return _parse_date(value.strip()) is not None


def _parse_date(value: str) -> Optional[_date]:
    for pattern, order in _DATE_PATTERNS:
        m = pattern.match(value)
        if not m:
            continue
        try:
            g = m.groups()
            if order == ('y3', 'm2', 'd1'):
                day, month, year = int(g[0]), int(g[1]), int(g[2])
            elif order == ('y1', 'm2', 'd3'):
                year, month, day = int(g[0]), int(g[1]), int(g[2])
            else:
                continue
            if year < 100:
                year += 2000
            return _date(year, month, day)
        except (ValueError, IndexError):
            continue
    return None


# 1C-style format presets: (id, label, group, hint_pattern, formatter)
def _fmt_date(d: _date, fmt_id: str) -> str:
    """Apply one of the named 1C-style date formats."""
    dd  = d.day
    mm  = d.month
    yy  = d.year
    dds = f'{dd:02d}'
    mms = f'{mm:02d}'
    yys = str(yy)
    yy2 = yys[-2:]
    wd  = d.weekday()

    fmts: dict[str, str] = {
        # ── Numeric (ДФ=) ────────────────────────────────────────────────────
        'дд.мм.гггг':   f'{dds}.{mms}.{yys}',
        'дд.мм.гг':     f'{dds}.{mms}.{yy2}',
        'дд/мм/гггг':   f'{dds}/{mms}/{yys}',
        'гггг-мм-дд':   f'{yys}-{mms}-{dds}',

        # ── Russian short (МММ = abbreviated month) ──────────────────────────
        'дд ммм гггг г.': f'{dd} {_RU_MONTHS_SHORT[mm]} {yys} г.',

        # ── Russian full (ММММ = full month in genitive) ─────────────────────
        'дд мммм гггг':        f'{dd} {_RU_MONTHS_GENT[mm]} {yys}',
        'дд мммм гггг г.':     f'{dd} {_RU_MONTHS_GENT[mm]} {yys} г.',
        'дд мммм гггг года':   f'{dd} {_RU_MONTHS_GENT[mm]} {yys} года',
        '«дд» мммм гггг г.':  f'«{dds}» {_RU_MONTHS_GENT[mm]} {yys} г.',
        '«дд» мммм гггг года': f'«{dds}» {_RU_MONTHS_GENT[mm]} {yys} года',

        # ── Russian with day of week ─────────────────────────────────────────
        'дддд, дд мммм гггг':  f'{_RU_WEEKDAYS_FULL[wd].capitalize()}, {dd} {_RU_MONTHS_GENT[mm]} {yys}',
        'ддд дд.мм.гггг':      f'{_RU_WEEKDAYS_SHORT[wd]}, {dds}.{mms}.{yys}',

        # ── Month + year only ────────────────────────────────────────────────
        'мммм гггг года':  f'{_RU_MONTHS_NOM[mm].capitalize()} {yys} года',
        'мм.гггг':         f'{mms}.{yys}',

        # ── Kazakh ──────────────────────────────────────────────────────────
        'дд мммм гггг ж.':   f'{dd} {_KK_MONTHS[mm]} {yys} ж.',
        'дд мммм гггг жыл':  f'{dd} {_KK_MONTHS[mm]} {yys} жыл',
        'дд.мм.гггг (қаз)':  f'{dds}.{mms}.{yys}',  # numeric same as RU
        'дддд (қаз)':        f'{_KK_WEEKDAYS[wd].capitalize()}',
    }
    return fmts.get(fmt_id, str(d))


# Ordered presets shown in the UI
_DATE_PRESETS: list[tuple[str, str, str]] = [
    # (fmt_id,               label,                        group)
    ('дд.мм.гггг',           'дд.ММ.гггг  →  10.04.2026',            'Числовой'),
    ('дд.мм.гг',             'дд.ММ.гг  →  10.04.26',                'Числовой'),
    ('дд/мм/гггг',           'дд/ММ/гггг  →  10/04/2026',            'Числовой'),
    ('гггг-мм-дд',           'гггг-ММ-дд  →  2026-04-10',            'Числовой'),
    ('дд ммм гггг г.',       'дд МММ гггг г.  (сокращ.)',             'Русский'),
    ('дд мммм гггг',         'дд ММММ гггг',                          'Русский'),
    ('дд мммм гггг г.',      'дд ММММ гггг г.',                       'Русский'),
    ('дд мммм гггг года',    'дд ММММ гггг года  (приказы)',          'Русский'),
    ('«дд» мммм гггг г.',   '«дд» ММММ гггг г.  (официальный)',      'Русский'),
    ('«дд» мммм гггг года', '«дд» ММММ гггг года',                   'Русский'),
    ('дддд, дд мммм гггг',   'дддд, дд ММММ гггг  (с днём недели)',  'Русский'),
    ('мммм гггг года',       'ММММ гггг года  (только месяц)',        'Русский'),
    ('дд мммм гггг ж.',      'дд ММММ гггг ж.  (қазақша)',           'Қазақша'),
    ('дд мммм гггг жыл',     'дд ММММ гггг жыл  (қазақша)',          'Қазақша'),
    ('дддд (қаз)',            'Күн атауы  (қазақша)',                  'Қазақша'),
]


def форматировать_дату(value: str, fmt_id: str) -> str:
    d = _parse_date(value.strip())
    if d is None:
        return value
    return _fmt_date(d, fmt_id)


def get_date_transforms(value: str) -> list[dict]:
    d = _parse_date(value.strip())
    if d is None:
        return []
    out = []
    for fmt_id, label, group in _DATE_PRESETS:
        result = _fmt_date(d, fmt_id)
        out.append({'id': f'дата:{fmt_id}', 'group': f'Дата — {group}',
                    'label': label, 'hint': '', 'result': result})
    return out


# ── gender detection ──────────────────────────────────────────────────────────

def определить_пол(full_name: str) -> Optional[str]:
    """Return 'м' (male) or 'ж' (female) from a Russian full name, or None."""
    # Most reliable: patronymic suffix
    for word in full_name.split():
        wl = word.lower()
        if wl.endswith(('ович', 'евич', 'ьич')):
            return 'м'
        if wl.endswith(('овна', 'евна', 'ична', 'инична')):
            return 'ж'
    # Fallback: pymorphy2 gender on any name-tagged word
    if _HAS_PM:
        for word in full_name.split():
            for p in _morph.parse(word):
                tag = str(p.tag)
                if ('Name' in tag or 'Patr' in tag or 'Surn' in tag) and p.tag.gender:
                    return 'м' if p.tag.gender == 'masc' else 'ж'
    return None


# ── main API ──────────────────────────────────────────────────────────────────

def apply_transform(fn_id: str, value: str) -> str:
    fn = fn_id.lower().strip()
    # Date transforms use "дата:<fmt_id>" prefix
    if fn.startswith('дата:'):
        return форматировать_дату(value, fn[5:])
    dispatch = {
        'числопрописью':  lambda: число_прописью(value, 'м'),
        'числопрописьюж': lambda: число_прописью(value, 'ж'),
        'числопрописьюс': lambda: число_прописью(value, 'с'),
        'валютакзт':      lambda: валюта_прописью(value, 'KZT'),
        'валютаруб':      lambda: валюта_прописью(value, 'RUB'),
        'валютауsd':      lambda: валюта_прописью(value, 'USD'),
        'валютаeur':      lambda: валюта_прописью(value, 'EUR'),
        'родительный':    lambda: склонить(value, 'рд'),
        'дательный':      lambda: склонить(value, 'дт'),
        'винительный':    lambda: склонить(value, 'вн'),
        'творительный':   lambda: склонить(value, 'тв'),
        'предложный':     lambda: склонить(value, 'пр'),
    }
    return dispatch.get(fn, lambda: value)()


def get_transforms(value: str) -> list[dict]:
    """
    Return a list of applicable transform descriptors for value.
    Each item: {id, label, hint, result, group}
    """
    out: list[dict] = []
    v = value.strip()

    # ── date transforms (checked first — a date like "10.04.2026" also matches is_number) ──
    if is_date(v):
        out += get_date_transforms(v)
        return out  # don't offer number/declension for pure dates

    # ── number transforms ────────────────────────────────────────────────────
    if is_number(v):
        m_words = число_прописью(v, 'м')
        f_words = число_прописью(v, 'ж')

        out.append({'id': 'числопрописью',  'group': 'Числа',
                    'label': 'Число прописью (м.р.)',
                    'hint': 'один, два, три…', 'result': m_words})
        if f_words != m_words:
            out.append({'id': 'числопрописьюж', 'group': 'Числа',
                        'label': 'Число прописью (ж.р.)',
                        'hint': 'одна, две…', 'result': f_words})

        for code, label in [('KZT', 'Тенге (тиын)'),
                             ('RUB', 'Рубли (коп.)'),
                             ('USD', 'Доллары (цент)'),
                             ('EUR', 'Евро (цент)')]:
            out.append({'id': f'валюта{code.lower()}', 'group': 'Валюта',
                        'label': label,
                        'hint': 'одна тысяча … тенге …',
                        'result': валюта_прописью(v, code)})

    # ── declension transforms ─────────────────────────────────────────────────
    if _HAS_PM and v and not is_number(v):
        for pycase, (case_label, case_hint) in CASE_LABELS.items():
            result = склонить(v, pycase)
            if result.lower() != v.lower():
                out.append({'id': pycase.replace('gent', 'родительный')
                                        .replace('datv', 'дательный')
                                        .replace('accs', 'винительный')
                                        .replace('ablt', 'творительный')
                                        .replace('loct', 'предложный'),
                            'group': 'Падежи',
                            'label': f'{case_label} ({case_hint})',
                            'hint': case_hint,
                            'result': result})

    # ── gender info for names ────────────────────────────────────────────────
    if is_name(v):
        gender = определить_пол(v)
        if gender:
            label = 'мужской род' if gender == 'м' else 'женский род'
            out.append({'id': 'пол', 'group': 'Имя',
                        'label': f'Пол: {label}',
                        'hint': 'определён автоматически',
                        'result': gender, 'info_only': True})

    return out
