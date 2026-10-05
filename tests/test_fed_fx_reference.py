import unittest
from research_base.fed_fx_reference import normalize_h10


def html(rows):
    return ('Release Date: Monday, January 9, 2006 (Rates in Chinese yuan per U.S. dollar)'
            '<table title="Historical Rates for the Chinese Renminbi">' + ''.join(
        '<tr><th>'+d+'</th><td>'+v+'</td></tr>' for d,v in rows) + '</table>')


class H10Controls(unittest.TestCase):
    def test_known_answer_units_missing_and_no_invented_availability(self):
        r = normalize_h10(html([('3-JAN-05','8.2765'),('4-JAN-05','ND'),('5-JAN-05','8.2768')]))
        self.assertEqual((r['row_count'],r['missing_count'],r['unit']), (3,1,'CNY_per_USD'))
        self.assertEqual(r['rows'][0]['date'],'2005-01-03')
        self.assertIsNone(r['rows'][1]['cny_per_usd'])
        self.assertFalse(r['historical_availability_verified'])
        self.assertFalse(r['original_S02_input_ready'])

    def test_wrong_units_or_ambiguous_tables_rejected(self):
        s = html([('3-JAN-05','8.2765')])
        with self.assertRaises(ValueError): normalize_h10(s.replace('Chinese yuan per U.S. dollar','U.S. dollars per yuan'))
        with self.assertRaises(ValueError): normalize_h10(s+s)

    def test_duplicate_backward_and_future_dates_rejected(self):
        for rows in ([('3-JAN-05','8'),('3-JAN-05','8')], [('4-JAN-05','8'),('3-JAN-05','8')], [('10-JAN-06','8')]):
            with self.subTest(rows=rows), self.assertRaises(ValueError): normalize_h10(html(rows))

    def test_nonpositive_nonfinite_or_bad_rates_rejected(self):
        for rate in ('0','-1','NaN','Infinity','bad'):
            with self.subTest(rate=rate), self.assertRaises(ValueError): normalize_h10(html([('3-JAN-05',rate)]))
