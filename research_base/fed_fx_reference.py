"""Normalize an acquired Board H.10 CNY reference table, without network I/O.

These are latest-vintage New York noon reference observations. Their labels
are not historical availability timestamps or executable retail FX quotes.
"""
from datetime import date
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import re


class _Table(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.cell = None
        self.rows = []
        self.row = None
        self.count = 0

    def handle_starttag(self, tag, attrs):
        if tag == 'table' and 'Historical Rates for the Chinese Renminbi' in dict(attrs).get('title', ''):
            self.active = True
            self.count += 1
        if not self.active: return
        if tag == 'tr': self.row = []
        if tag in ('th', 'td') and self.row is not None: self.cell = ''

    def handle_data(self, data):
        if self.cell is not None: self.cell += data

    def handle_endtag(self, tag):
        if not self.active: return
        if tag in ('th', 'td') and self.cell is not None:
            self.row.append(self.cell.strip())
            self.cell = None
        if tag == 'tr' and self.row is not None:
            self.rows.append(self.row)
            self.row = None
        if tag == 'table': self.active = False


def normalize_h10(html):
    if not isinstance(html, str) or '(Rates in Chinese yuan per U.S. dollar)' not in html:
        raise ValueError('EXPLICIT_CNY_PER_USD_SOURCE_UNIT_REQUIRED')
    release = re.search(r'Release Date:\s*\w+,\s*(\w+)\s+(\d{1,2}),\s*(\d{4})', html)
    if not release:
        raise ValueError('SOURCE_RELEASE_DATE_REQUIRED')
    names = ['January','February','March','April','May','June','July','August','September','October','November','December']
    release_day = date(int(release[3]), names.index(release[1])+1, int(release[2]))
    parser = _Table()
    parser.feed(html)
    if parser.count != 1 or not parser.rows:
        raise ValueError('EXACT_ONE_BOARD_CNY_HISTORY_TABLE_REQUIRED')
    months = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC']
    rows, seen, previous = [], set(), None
    for cells in parser.rows:
        if len(cells) != 2:
            raise ValueError('EXACT_TWO_CNY_HISTORY_COLUMNS_REQUIRED')
        label, raw = cells
        match = re.fullmatch(r'(\d{1,2})-([A-Z]{3})-(\d{2})', label)
        if not match:
            raise ValueError('EXPECTED_2000_ONWARD_BOARD_DATE_LABEL')
        day = date(2000+int(match[3]), months.index(match[2])+1, int(match[1]))
        if day > release_day or day in seen or (previous and day <= previous):
            raise ValueError('FUTURE_DUPLICATE_OR_UNSORTED_FX_OBSERVATION')
        seen.add(day)
        previous = day
        value = None
        if raw != 'ND':
            try: value = Decimal(raw)
            except InvalidOperation as exc: raise ValueError('INVALID_CNY_REFERENCE_RATE') from exc
            if not value.is_finite() or value <= 0:
                raise ValueError('INVALID_CNY_REFERENCE_RATE')
        rows.append({'date': day.isoformat(), 'cny_per_usd': str(value) if value is not None else None,
                     'missing': raw == 'ND', 'historical_known_at': None})
    valid = [r for r in rows if r['cny_per_usd'] is not None]
    if not valid: raise ValueError('NO_VALID_FX_OBSERVATIONS')
    return {'schema_version': 'fed-h10-cny-reference/1', 'rows': rows,
            'source_release_date': release_day.isoformat(), 'first_observation': rows[0]['date'],
            'last_observation': rows[-1]['date'], 'last_valid_observation': valid[-1]['date'],
            'row_count': len(rows), 'missing_count': sum(r['missing'] for r in rows),
            'unit': 'CNY_per_USD', 'observation_basis': 'New_York_noon_cable_transfer_buying_reference',
            'vintage': 'latest_at_acquisition_not_point_in_time_archive',
            'historical_availability_verified': False, 'retail_executable_fx_verified': False,
            'original_S02_input_ready': False, 'goal_complete': False}
