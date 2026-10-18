# Synthetic fixtures | pipeline checks | UI smoke tests
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest
from app import eligible, load_data, street_summary, street_layer
from pipeline import FIELDS, merge_csvs, process, fingerprint

ROOT = Path(__file__).resolve().parent


def sample(identifier='1', price='50 M Ft', address='Pécs, Próba utca'):
    stream = io.StringIO()
    writer = csv.writer(stream, delimiter=';')
    writer.writerow(FIELDS)
    writer.writerow([identifier, 'https://example.org/' + identifier, address,
                     price, '1 000 000 Ft/m²', '50 m²', '2', ''])
    return stream.getvalue().rstrip('\r\n').encode('utf-8-sig')


class Tests(unittest.TestCase):
    def test_merge_without_final_newline_and_reordered_columns(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'merged.csv'
            manifest = merge_csvs([('1.csv', sample()), ('2.csv', sample('2'))], target)
            with target.open(encoding='utf-8-sig') as f:
                self.assertEqual(len(list(csv.DictReader(f, delimiter=';'))), 2)
            self.assertEqual(manifest[1]['merged_start_row'], 3)
            with self.assertRaises(ValueError):
                merge_csvs([('broken.csv', b'a;b\n1;2')], target)

    def test_initial_app_has_no_data(self):
        at = AppTest.from_file(str(ROOT / 'app.py')).run(timeout=30)
        self.assertFalse(at.exception)
        self.assertEqual(len(at.metric), 0)
        self.assertEqual(len(at.dataframe), 0)
        self.assertTrue(at.sidebar.button[0].disabled)
        self.assertEqual(len(at.get('deck_gl_json_chart')), 1)

    def test_pipeline_duplicate_and_ui(self):
        osm = b'''<osm version="0.6"><node id="1" lat="46.07" lon="18.23"><tag k="addr:city" v="P\xc3\xa9cs"/><tag k="addr:street" v="Pr\xc3\xb3ba utca"/><tag k="addr:housenumber" v="1"/></node><node id="2" lat="46.071" lon="18.23"/><way id="3"><nd ref="1"/><nd ref="2"/><tag k="highway" v="residential"/><tag k="name" v="Pr\xc3\xb3ba utca"/></way></osm>'''
        files = [('a.csv', sample()), ('b.csv', sample()), ('c.csv', sample('1', '60 M Ft')),
                 ('d.csv', sample('2', '75 M Ft', 'Pécs, Próba utca 1.'))]
        result = process(files, osm_content=osm)
        self.assertEqual(result['report']['accepted_records'], 2)
        self.assertEqual(result['report']['ingestion_issues']['duplicate'], 1)
        self.assertEqual(result['report']['ingestion_issues']['conflicting_duplicate'], 1)
        self.assertEqual(len(result['geometry']['features']), 1)
        df = eligible(load_data(result['csv']))
        low = street_summary(df, (1300000, 1600000))
        high = street_summary(df, (900000, 1100000))
        self.assertEqual(low.iloc[0].color[:3], [34, 139, 126])
        self.assertEqual(high.iloc[0].color[:3], [194, 66, 66])
        self.assertEqual(low.iloc[0].color[3], 90)
        self.assertEqual(json.loads(street_layer([]).to_json())['widthUnits'], 'pixels')
        # Seed a finished job to exercise the render path without a browser uploader.
        # No uploaded files are selected in AppTest, so use that input fingerprint.
        from app import default_osm
        path = default_osm()
        result['fingerprint'] = fingerprint([], osm_path=path or None)
        at = AppTest.from_file(str(ROOT / 'app.py'))
        at.session_state['result'] = result
        at.run(timeout=30)
        self.assertFalse(at.exception)
        self.assertEqual(at.metric[0].value, '2')
        at.sidebar.slider[0].set_range(1300000, 1500000).run()
        self.assertFalse(at.exception)
        at.sidebar.number_input[-1].set_value(3).run()
        self.assertFalse(at.exception)
        self.assertTrue(any('minimum hirdetésszám' in w.value for w in at.warning))
        at.sidebar.button[1].click().run()
        self.assertFalse(at.exception)
        self.assertEqual(len(at.metric), 0)


if __name__ == '__main__':
    unittest.main()
