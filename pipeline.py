"""Local CSV → geocoding → street geometry pipeline; no website requests."""
# Input validation | CSV merge | temporary files | result export
import csv
import hashlib
import io
import json
import tempfile
import zipfile
from pathlib import Path

from geokodolo import run
from build_streets import build

FIELDS = ['hirdetes_id', 'link', 'cim', 'ar', 'ar_m2', 'alapterulet', 'szobak', 'erkely']


def merge_csvs(files, output):
    """Parse each file independently, retaining duplicates for the ingest audit."""
    manifest, total = [], 0
    with output.open('w', encoding='utf-8-sig', newline='') as target:
        writer = csv.DictWriter(target, fieldnames=FIELDS, delimiter=';')
        writer.writeheader()
        for name, content in files:
            try:
                reader = csv.DictReader(io.StringIO(content.decode('utf-8-sig'), newline=''), delimiter=';', strict=True)
                if set(reader.fieldnames or []) != set(FIELDS):
                    raise ValueError('A nyers hirdetéskártya-CSV nyolc oszlopa szükséges.')
                count, start = 0, total + 2
                for number, row in enumerate(reader, 2):
                    if None in row or any(value is None for value in row.values()):
                        raise ValueError(f'Hibás oszlopszám a(z) {number}. sorban.')
                    if not any(value.strip() for value in row.values()):
                        continue
                    writer.writerow(row)
                    count += 1
                    total += 1
            except (UnicodeDecodeError, csv.Error, ValueError) as exc:
                raise ValueError(f'{name}: {exc}') from exc
            manifest.append({'filename': name, 'records': count, 'merged_start_row': start,
                             'sha256': hashlib.sha256(content).hexdigest()})
    if not total:
        raise ValueError('A feltöltött CSV-k nem tartalmaznak hirdetéseket.')
    return manifest


def fingerprint(files, osm_content=None, osm_path=None):
    h = hashlib.sha256()
    for name, content in files:
        h.update(name.encode()); h.update(hashlib.sha256(content).digest())
    if osm_content is not None:
        h.update(hashlib.sha256(osm_content).digest())
    elif osm_path:
        p = Path(osm_path)
        h.update(str(p.resolve()).encode())
        stat = p.stat()
        h.update(f'{stat.st_mtime_ns}:{stat.st_size}'.encode())
    return h.hexdigest()


def process(files, osm_path=None, osm_content=None, progress=lambda value, label: None):
    """Publish results only on complete success; temporary input files are removed."""
    if osm_content is None and (osm_path is None or not Path(osm_path).is_file()):
        raise ValueError('Adj meg egy létező OSM XML-fájlt.')
    with tempfile.TemporaryDirectory(prefix='pecs-webapp-') as folder:
        root = Path(folder)
        progress(10, 'CSV-k ellenőrzése és összevonása…')
        merged = root / 'merged.csv'
        manifest = merge_csvs(files, merged)
        if osm_content is not None:
            osm = root / 'reference.osm'
            osm.write_bytes(osm_content)
        else:
            osm = Path(osm_path)
        progress(25, 'OSM feldolgozása és címillesztés…')
        output = root / 'results'
        report = run(merged, osm, output)
        if not report['accepted_records']:
            raise ValueError('Nincs feldolgozható pécsi hirdetés. Ellenőrizd a cím-, ár- és területadatokat.')
        progress(75, 'Utcavonalak összeállítása…')
        build(osm, output / 'listings_geocoded.csv', output / 'streets.geojson')
        report['input_files'] = manifest
        report['limitations'][0] = 'User-provided asking-price sample; not representative of the market.'
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        progress(90, 'Eredmények és letöltések előkészítése…')
        exports = {p.name: p.read_bytes() for p in output.iterdir() if p.is_file()}
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, content in exports.items():
                archive.writestr(name, content)
        progress(100, 'Feldolgozás kész.')
        return {'csv': exports['listings_geocoded.csv'], 'geometry': json.loads(exports['streets.geojson']),
                'report': report, 'exports': exports, 'zip': stream.getvalue()}
