"""Adatminőségi jelzések; egyik jelzés sem jelent automatikus törlést."""
# Data quality | outliers | review flags
import numpy as np
import pandas as pd


def inspect_data(df):
    result = df.copy()
    price = pd.to_numeric(result.asking_price_huf, errors='coerce')
    area = pd.to_numeric(result.area_m2, errors='coerce')
    valid = price.gt(0) & area.gt(0) & np.isfinite(price) & np.isfinite(area)
    result['szamitott_Ft_m2'] = (price / area).where(valid)
    # Ugyanaz az utca/ár/terület több különböző lakást is jelenthet.
    result['lehetseges_tobbszoros_hirdetes'] = result.duplicated(
        ['address_raw', 'asking_price_huf', 'area_m2'], keep=False)
    result['magyarazatlan_areltérés'] = result.get(
        'unit_price_explanation', pd.Series('', index=result.index)).eq('unexplained_difference')
    result['ervenytelen_ar_vagy_terulet'] = ~valid
    # IQR-szabály: ellenőrzési jelzés, nem hibamegállapítás.
    q1, q3 = result.szamitott_Ft_m2.quantile([.25, .75])
    iqr = q3 - q1
    result['szelso_negyzetmeterar'] = False
    if pd.notna(iqr) and iqr > 0:
        result['szelso_negyzetmeterar'] = (result.szamitott_Ft_m2.lt(q1 - 1.5 * iqr)
                                                   | result.szamitott_Ft_m2.gt(q3 + 1.5 * iqr))
    return result


def validation_sample(df, per_status=20):
    samples = []
    for status in ['address_matched', 'street_approximate']:
        group = df[df.geocode_status.eq(status)].sort_values('listing_id')
        samples.append(group.sample(n=min(per_status, len(group)), random_state=42))
    sample = pd.concat(samples).copy()
    sample['ellenorzes_eredmenye'] = ''
    sample['ellenorzes_forrasa'] = ''
    sample['ellenorzes_megjegyzes'] = ''
    return sample


def csv_bytes(df):
    return df.to_csv(index=False, sep=';').encode('utf-8-sig')
