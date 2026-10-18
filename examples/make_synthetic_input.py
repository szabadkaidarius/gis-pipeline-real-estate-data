"""Generate local test input — no third-party data."""
# Synthetic records | documented input schema | offline sample
import csv
from pathlib import Path

HEADERS = ['hirdetes_id', 'link', 'cim', 'ar', 'ar_m2', 'alapterulet', 'szobak', 'erkely']
ROWS = [
    ['demo-001', 'https://example.org/demo-001', 'Pécs, Próba utca 1.', '50 M Ft', '1 000 000 Ft/m²', '50 m²', '2', ''],
    ['demo-002', 'https://example.org/demo-002', 'Pécs, Próba utca', '60 M Ft', '1 000 000 Ft/m²', '60 m²', '3', ''],
    ['demo-003', 'https://example.org/demo-003', 'Pécs, Próba utca 1.', '72 M Ft', '1 200 000 Ft/m²', '60 m²', '3', ''],
]
output = Path(__file__).resolve().parent / 'synthetic_listings.csv'
with output.open('w', encoding='utf-8-sig', newline='') as handle:
    writer = csv.writer(handle, delimiter=';')
    writer.writerow(HEADERS)
    writer.writerows(ROWS)
print('Created:', output)
