"""Offline OSM geocoding with explicit uncertainty. Python 3.10+."""
# Offline geocoding | matching quality | ambiguity tracking
import argparse
import csv
import hashlib
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import LineString, Point, Polygon, mapping
from shapely.ops import transform, unary_union

# Pécs-specific projected CRS. Metric operations never use angular coordinates.
FORWARD = Transformer.from_crs(4326, 32634, always_xy=True).transform
INVERSE = Transformer.from_crs(32634, 4326, always_xy=True).transform
STREET_TYPES = ['utca', 'utcája', 'út', 'útja', 'tér', 'főtér', 'köz', 'sétány',
                'körút', 'sor', 'dűlő', 'rakpart', 'lejtő', 'fasor', 'liget', 'park']
NUMBER = r'\d+(?:\s*[/.]?\s*[A-Za-z]|\s*/\s*\d+)?(?:\s*[-–]\s*\d+(?:\s*/?\s*[A-Za-z])?)?'


def clean(value):
    return ' '.join(unicodedata.normalize('NFKC', value or '').split())


def key(value):
    # Preserve accents and spelling. No fuzzy matching in this release.
    return clean(value).casefold()


def house_key(value):
    return re.sub(r'\s+', '', clean(value)).upper().replace('–', '-').rstrip('.')


def address(value):
    city, separator, remainder = clean(value).partition(',')
    remainder = remainder.strip()
    if not separator or not city or not remainder:
        raise ValueError('A cím formátuma: település, közterület vagy városrész')
    types = '|'.join(re.escape(t) for t in STREET_TYPES)
    match = re.fullmatch(rf'(.+\b(?:{types}))\s+({NUMBER})\.?', remainder, re.I)
    if match:
        street, number = match.groups()
        number = house_key(number)
        return city, street, number, 'house_number_range' if '-' in number else 'house_number'
    level = 'street' if key(remainder.split()[-1].rstrip('.')) in STREET_TYPES else 'locality_or_unparsed'
    return city, remainder, '', level


def number(value):
    text = clean(value).replace(' ', '').replace(',', '.')
    if not re.fullmatch(r'\d+(?:\.\d+)?', text):
        raise ValueError(f'Nem értelmezhető szám: {value!r}')
    return float(text)


def measure(value, unit):
    text = clean(value)
    pattern = r'([\d\s.,]+)\s*' + unit
    match = re.fullmatch(pattern, text, re.I)
    if not match:
        raise ValueError(f'Nem értelmezhető mennyiség: {value!r}')
    return number(match[1])


def price(value):
    match = re.fullmatch(r'([\d\s.,]+)\s*(M)?\s*Ft', clean(value), re.I)
    if not match:
        raise ValueError(f'Nem értelmezhető ár: {value!r}')
    return number(match[1]) * (1_000_000 if match[2] else 1)


def load_listings(path, city):
    listings, issues, seen = [], [], {}
    with Path(path).open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle, delimiter=';', strict=True)
        required = {'hirdetes_id', 'link', 'cim', 'ar', 'ar_m2', 'alapterulet', 'szobak', 'erkely'}
        if not required <= set(reader.fieldnames or []):
            raise ValueError('A CSV nem a scrape_pecs.py oszlopszerkezetét használja.')
        for row_number, row in enumerate(reader, 2):
            try:
                if None in row or any(v is None for v in row.values()):
                    raise ValueError('Eltérő CSV-oszlopszám')
                identifier = row['hirdetes_id'].strip()
                if not identifier:
                    raise ValueError('Hiányzó hirdetésazonosító')
                if identifier in seen:
                    state = 'duplicate' if row == seen[identifier] else 'conflicting_duplicate'
                    issues.append(dict(source_row=row_number, listing_id=identifier, status=state, reason='Az első érvényes rekord maradt meg.'))
                    continue
                town, street, house, level = address(row['cim'])
                if key(town) != key(city):
                    issues.append(dict(source_row=row_number, listing_id=identifier, status='excluded_city', reason=town))
                    continue
                asking = price(row['ar'])
                area = measure(row['alapterulet'], r'm[²2]')
                balcony = measure(row['erkely'], r'm[²2]') if clean(row['erkely']) else None
                reported = measure(row['ar_m2'], r'Ft\s*/\s*m[²2]') if clean(row['ar_m2']) else None
                if area <= 0 or asking <= 0:
                    raise ValueError('Az ár és az alapterület legyen pozitív')
                calculated = asking / area
                adjusted = asking / (area + balcony / 2) if balcony is not None else None
                explanation = 'not_reported'
                if reported is not None:
                    if abs(reported - calculated) <= max(2, calculated * .00001):
                        explanation = 'matches_floor_area'
                    elif adjusted is not None and abs(reported - adjusted) <= max(2, adjusted * .00001):
                        explanation = 'consistent_with_half_balcony_hypothesis'
                    else:
                        explanation = 'unexplained_difference'
                listings.append(dict(listing_id=identifier, source_url=row['link'], source_row=row_number,
                    address_raw=row['cim'], city=town, street=street, house_number=house,
                    address_level=level, asking_price_huf=asking, area_m2=area,
                    balcony_m2=balcony, rooms_raw=row['szobak'], price_m2_floor=round(calculated, 2),
                    price_m2_reported=reported, price_m2_half_balcony=round(adjusted, 2) if adjusted is not None else None,
                    unit_price_explanation=explanation))
                seen[identifier] = row
            except ValueError as error:
                issues.append(dict(source_row=row_number, listing_id=row.get('hirdetes_id', ''), status='rejected', reason=str(error)))
    return listings, issues


def read_osm(path):
    nodes, ways, relations = {}, {}, []
    metadata = {}
    for _, element in ET.iterparse(path, events=('end',)):
        kind = element.tag
        if kind == 'meta':
            metadata['osm_base'] = element.get('osm_base')
        elif kind == 'bounds':
            metadata['bounds'] = dict(element.attrib)
        elif kind in ('node', 'way', 'relation'):
            tags = {t.get('k'): t.get('v') for t in element.findall('tag')}
            identifier = element.get('id')
            if kind == 'node':
                nodes[identifier] = (float(element.get('lon')), float(element.get('lat')), tags)
            elif kind == 'way':
                ways[identifier] = ([n.get('ref') for n in element.findall('nd')], tags)
            else:
                relations.append((identifier, tags, [dict(m.attrib) for m in element.findall('member')]))
            element.clear()
    return nodes, ways, relations, metadata


def reference(nodes, ways, relations, city, needed_streets):
    exact, streets, anchors = defaultdict(list), defaultdict(list), defaultdict(list)
    diagnostics = Counter()

    def add_address(kind, identifier, tags, point):
        street = key(tags.get('addr:street'))
        if key(tags.get('addr:city')) != key(city) or not street:
            return
        anchors[street].append(point)
        if tags.get('addr:housenumber'):
            exact[(street, house_key(tags['addr:housenumber']))].append(
                dict(osm_id=f'{kind}/{identifier}', method='address_node' if kind == 'node' else 'address_way_internal_point', point=point))

    for identifier, (lon, lat, tags) in nodes.items():
        add_address('node', identifier, tags, Point(lon, lat))
    for identifier, (refs, tags) in ways.items():
        relevant = 'addr:street' in tags or (tags.get('highway') and key(tags.get('name')) in needed_streets)
        if not relevant:
            continue
        if any(ref not in nodes for ref in refs):
            diagnostics['ways_missing_nodes'] += 1
            continue
        coordinates = [nodes[ref][:2] for ref in refs]
        if len(coordinates) < 2:
            continue
        if tags.get('addr:street') and tags.get('addr:housenumber'):
            if len(coordinates) >= 4 and refs[0] == refs[-1]:
                polygon = transform(FORWARD, Polygon(coordinates))
                if polygon.is_valid and polygon.area > 0:
                    add_address('way', identifier, tags, transform(INVERSE, polygon.representative_point()))
                else:
                    diagnostics['invalid_address_polygons'] += 1
            else:
                diagnostics['nonpolygon_address_ways_skipped'] += 1
        if tags.get('highway') and key(tags.get('name')) in needed_streets:
            streets[key(tags['name'])].append((f'way/{identifier}', transform(FORWARD, LineString(coordinates))))

    # This extract has an incomplete municipal boundary. Do not label every named
    # highway in the regional extract as belonging to Pécs. Retain only segments
    # within 100 m of explicit Pécs address anchors of the same street name.
    scoped_streets = {}
    for name, candidates in streets.items():
        if not anchors[name]:
            diagnostics['streets_without_city_address_anchors'] += 1
            continue
        anchor_geometry = unary_union([transform(FORWARD, p) for p in anchors[name]])
        accepted = [(identifier, line) for identifier, line in candidates if line.distance(anchor_geometry) <= 100]
        if accepted:
            scoped_streets[name] = accepted
    diagnostics['address_relations_not_supported'] = sum(bool(t.get('addr:street') and t.get('addr:housenumber')) for _, t, _ in relations)
    for identifier, tags, members in relations:
        if tags.get('boundary') == 'administrative' and key(tags.get('name')) == key(city):
            boundary_ways = [m['ref'] for m in members if m['type'] == 'way']
            diagnostics['municipal_boundary_missing_ways'] = sum(r not in ways for r in boundary_ways)
    return exact, scoped_streets, dict(diagnostics)


def match_listing(row, exact, streets):
    result = dict(row, geocode_status='unmatched', geocode_method='', geocode_reason='',
                  candidate_count=0, osm_ids='', longitude=None, latitude=None)
    point, line_geometry = None, None
    name = key(row['street'])
    if row['address_level'] in ('house_number', 'house_number_range'):
        candidates = exact.get((name, house_key(row['house_number'])), [])
        result['candidate_count'] = len(candidates)
        result['osm_ids'] = '|'.join(c['osm_id'] for c in candidates)
        if len(candidates) == 1:
            point = candidates[0]['point']
            result.update(geocode_status='address_matched', geocode_method=candidates[0]['method'], geocode_reason='Egyetlen, azonos város/utca/házszám OSM-objektum. Nem igazolja a hirdetés tényleges helyét.')
        elif candidates:
            result.update(geocode_status='ambiguous', geocode_reason='Több OSM-címobjektum; nincs önkényes koordinátaválasztás.')
        else:
            result['geocode_reason'] = 'Nincs pontos címkulcsegyezés. A házszámot nem töröltük és nem közelítettük utcára.'
    elif row['address_level'] == 'street':
        candidates = streets.get(name, [])
        result['candidate_count'] = len(candidates)
        result['osm_ids'] = '|'.join(identifier for identifier, _ in candidates)
        if candidates:
            merged = unary_union([g for _, g in candidates])
            lines = list(merged.geoms) if merged.geom_type == 'MultiLineString' else [merged]
            longest = max(lines, key=lambda g: g.length)
            point = transform(INVERSE, longest.interpolate(.5, normalized=True))
            line_geometry = transform(INVERSE, merged)
            result.update(geocode_status='street_approximate', geocode_method='midpoint_of_longest_scoped_segment', geocode_reason='Utcaszintű szemléltető pont; csak Pécs-címhorgonyokkal alátámasztott útszakaszok. Nem ingatlanhely és nem a teljes utca közepe.')
        else:
            result['geocode_reason'] = 'Nincs azonos nevű, Pécs-címhorgonyokkal alátámasztott útszakasz.'
    else:
        result['geocode_reason'] = 'Városrész vagy nem értelmezett cím; nincs kitalált koordináta.'
    if point is not None:
        result['longitude'], result['latitude'] = point.x, point.y
    return result, point, line_geometry


def write_csv(path, rows, fields):
    with path.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter=';')
        writer.writeheader()
        writer.writerows(rows)


def write_geojson(path, features):
    path.write_text(json.dumps({'type': 'FeatureCollection', 'features': features}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def run(csv_path, osm_path, output, city='Pécs'):
    if key(city) != key('Pécs'):
        raise ValueError('Ez a verzió Pécsre ellenőrzött; más városhoz a vetület és területi szabály felülvizsgálandó.')
    if output.exists():
        raise ValueError('A kimeneti mappa már létezik. Adj meg új mappanevet.')
    listings, issues = load_listings(csv_path, city)
    nodes, ways, relations, metadata = read_osm(osm_path)
    exact, streets, diagnostics = reference(nodes, ways, relations, city, {key(r['street']) for r in listings})
    results, address_points, street_points, street_lines = [], [], [], []
    for listing in listings:
        result, point, lines = match_listing(listing, exact, streets)
        results.append(result)
        if point is not None:
            feature = dict(type='Feature', geometry=mapping(point), properties=result)
            (address_points if result['geocode_status'] == 'address_matched' else street_points).append(feature)
        if lines is not None:
            street_lines.append(dict(type='Feature', geometry=mapping(lines), properties=result))
    report = dict(input_records=len(listings) + len(issues), accepted_records=len(listings),
        ingestion_issues=dict(Counter(i['status'] for i in issues)),
        address_levels=dict(Counter(r['address_level'] for r in listings)),
        geocode_status=dict(Counter(r['geocode_status'] for r in results)),
        price_explanations=dict(Counter(r['unit_price_explanation'] for r in listings)),
        csv_sha256=hashlib.sha256(Path(csv_path).read_bytes()).hexdigest(),
        osm_sha256=hashlib.sha256(Path(osm_path).read_bytes()).hexdigest(), osm_metadata=metadata,
        reference_diagnostics=diagnostics, metric_crs='EPSG:32634', export_crs='EPSG:4326',
        street_anchor_distance_m=100, manually_validated=False,
        limitations=['User-provided asking-price sample; not representative of the market.',
            'OSM is a historical extract, not current ground truth.',
            'Street scope uses same-name explicit city address anchors; 100 m is a heuristic, not a municipal boundary.',
            'No fuzzy matching, suffix dropping, or expansion of house-number ranges.',
            'Address-bearing relations are counted but not geocoded.',
            'Half-balcony formula is checked as a hypothesis, not asserted as the source methodology.'])
    output.mkdir(parents=True)
    fields = list(results[0]) if results else ['listing_id', 'geocode_status', 'geocode_reason']
    write_csv(output / 'listings_geocoded.csv', results, fields)
    write_csv(output / 'review.csv', [r for r in results if r['geocode_status'] in ('ambiguous', 'unmatched')], fields)
    write_csv(output / 'ingestion_issues.csv', issues, ['source_row', 'listing_id', 'status', 'reason'])
    write_geojson(output / 'address_points.geojson', address_points)
    write_geojson(output / 'street_approximate_points.geojson', street_points)
    write_geojson(output / 'street_segments.geojson', street_lines)
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description='Offline Pécs listing geocoder with explicit uncertainty')
    parser.add_argument('--csv', type=Path, required=True)
    parser.add_argument('--osm', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        report = run(args.csv, args.osm, args.output)
    except (ValueError, OSError, csv.Error, ET.ParseError) as error:
        parser.exit(1, f'Hiba: {error}\n')
    print(json.dumps(report['geocode_status'], ensure_ascii=False, indent=2))
    print(f'Kimenet: {args.output.resolve()}')


if __name__ == '__main__':
    main()
