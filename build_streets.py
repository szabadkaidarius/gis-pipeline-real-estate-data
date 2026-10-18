"""Extract connected, same-name OSM street ways anchored by mapped listings."""
# OSM ways | street geometry | GeoJSON output
import argparse
import csv
import json
import math
import unicodedata
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path


def key(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def distance(point, coords):
    # Local equirectangular approximation for selecting nearby roads, not GIS analysis.
    scale = math.cos(math.radians(point[1]))
    xy = [((c[0]-point[0])*111320*scale, (c[1]-point[1])*111320) for c in coords]
    best = float('inf')
    for a, b in zip(xy, xy[1:]):
        dx, dy = b[0]-a[0], b[1]-a[1]
        t = max(0, min(1, -(a[0]*dx+a[1]*dy)/(dx*dx+dy*dy))) if dx*dx+dy*dy else 0
        best = min(best, math.hypot(a[0]+t*dx, a[1]+t*dy))
    return best


def build(osm, csv_path, output):
    with csv_path.open(encoding='utf-8-sig', newline='') as handle:
        rows = list(csv.DictReader(handle, delimiter=';'))
    anchors = defaultdict(list)
    for r in rows:
        if r['geocode_status'] in ('address_matched', 'street_approximate'):
            anchors[(r['city'], r['street'])].append((float(r['longitude']), float(r['latitude'])))
    needed = {key(street) for city, street in anchors}
    nodes, roads = {}, defaultdict(dict)
    for _, e in ET.iterparse(osm, events=['end']):
        if e.tag == 'node':
            nodes[e.get('id')] = (float(e.get('lon')), float(e.get('lat')))
        elif e.tag == 'way':
            tags = {t.get('k'):t.get('v') for t in e.findall('tag')}
            name = key(tags.get('name', ''))
            refs = [n.get('ref') for n in e.findall('nd')]
            if tags.get('highway') and name in needed and len(refs)>1 and all(n in nodes for n in refs):
                roads[name][e.get('id')] = refs
        elif e.tag != 'relation':
            continue
        e.clear()
    features = []
    missing = []
    for (city, street), points in sorted(anchors.items()):
        ways = roads[key(street)]
        by_node = defaultdict(set)
        for wid, refs in ways.items():
            for ref in refs:by_node[ref].add(wid)
        # Seed near the known same-name street/city matches, then follow all connected segments.
        seeds = {wid for wid, refs in ways.items() if any(distance(p, [nodes[n] for n in refs]) <= 150 for p in points)}
        accepted = set(seeds)
        pending = list(seeds)
        while pending:
            wid = pending.pop()
            for ref in ways[wid]:
                for neighbor in by_node[ref] - accepted:
                    accepted.add(neighbor); pending.append(neighbor)
        if not accepted:
            missing.append(street); continue
        features.append({'type':'Feature', 'properties':{'city':city, 'street':street, 'osm_way_ids':sorted(accepted)},
                         'geometry':{'type':'MultiLineString', 'coordinates':[[nodes[n] for n in ways[wid]] for wid in sorted(accepted)]}})
    result = {'type':'FeatureCollection', 'features':features,
              'metadata':{'method':'Same-name connected OSM ways, seeded within 150 m of existing matched coordinates.',
                          'limitations':'Extract extent, missing ways and disconnected segments can truncate streets. No complete municipal boundary validation.',
                          'missing_streets':missing}}
    output.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    print(f'{len(features)} utca vonalgeometriával; hiányzó: {missing}')


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--osm', type=Path, required=True)
    p.add_argument('--csv', type=Path, default=Path('data/listings_geocoded.csv'))
    p.add_argument('--output', type=Path, default=Path('data/streets.geojson'))
    args=p.parse_args(); build(args.osm,args.csv,args.output)
