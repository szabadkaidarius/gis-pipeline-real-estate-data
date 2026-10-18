"""Run: python -m streamlit run app.py"""
# UI layers | filters | exports | session state
from pathlib import Path
import io
import html
import json
from pipeline import process, fingerprint
from quality import inspect_data, validation_sample, csv_bytes
import numpy as np
import pandas as pd
import pydeck as pdk
import streamlit as st

ROOT = Path(__file__).resolve().parent
LABELS = {"address_matched": "Cím alapján illesztett", "street_approximate": "Utcaszintű közelítés"}


def load_data(content):
    df = pd.read_csv(io.BytesIO(content), sep=";", encoding="utf-8-sig", dtype={"listing_id": str})
    required = {"listing_id", "city", "street", "address_raw", "geocode_status", "longitude", "latitude", "asking_price_huf", "area_m2"}
    if not required.issubset(df.columns):
        raise ValueError("A geokódoló listings_geocoded.csv fájlja szükséges.")
    if df.listing_id.isna().any() or df.listing_id.duplicated().any():
        raise ValueError("Hiányzó vagy ismétlődő hirdetésazonosító.")
    for col in ["longitude", "latitude", "asking_price_huf", "area_m2"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def eligible(df):
    mask = (df.geocode_status.isin(LABELS) & df.longitude.between(-180, 180)
            & df.latitude.between(-90, 90) & df.asking_price_huf.gt(0)
            & df.area_m2.gt(0) & np.isfinite(df.asking_price_huf) & np.isfinite(df.area_m2))
    result = df[mask].copy()
    result["price_m2"] = result.asking_price_huf / result.area_m2
    result["price_million"] = result.asking_price_huf / 1e6
    result["pontossag"] = result.geocode_status.map(LABELS)
    return result


def street_summary(df, thresholds=(900000, 1100000)):
    low, high = thresholds
    # Coordinates are display anchors chosen from one real matched row, not averaged property locations.
    result = df.groupby(["city", "street"], dropna=False).agg(
        count=("listing_id", "size"), median_price_m2=("price_m2", "median"),
        median_price_million=("price_million", "median"),
        longitude=("longitude", "first"), latitude=("latitude", "first"),
        address_count=("geocode_status", lambda s: int((s == "address_matched").sum()))).reset_index()
    result["approx_count"] = result["count"] - result.address_count
    result["median_label"] = result.median_price_m2.map(lambda v: f"{v:,.0f} Ft/m²")
    result["street_safe"] = result.street.fillna("Ismeretlen").map(html.escape)
    result["color"] = result.median_price_m2.map(lambda v: [34, 139, 126, 200] if v < low else ([230, 162, 50, 200] if v < high else [194, 66, 66, 200]))
    result["color"] = [color[:3] + [90 if count < 3 else 200] for color, count in zip(result.color, result["count"])]
    result["sample_label"] = result["count"].map(lambda n: "Kis minta (1–2 hirdetés)" if n < 3 else "Legalább 3 hirdetés; reprezentativitás nem garantált")
    return result


def street_paths(summary, geojson):
    geometries = {(f['properties']['city'], f['properties']['street']): f['geometry']
                  for f in geojson['features']}
    paths, missing = [], []
    for row in summary.to_dict('records'):
        geometry = geometries.get((row['city'], row['street']))
        if geometry is None:
            missing.append(row['street']); continue
        lines = [geometry['coordinates']] if geometry['type'] == 'LineString' else geometry['coordinates']
        for line in lines:
            paths.append({**row, 'path': line})
    return paths, missing


def street_layer(paths):
    # PyDeck parses unquoted strings as expressions; enum values need literal quotes.
    return pdk.Layer("PathLayer", paths, get_path="path", get_color="color",
                     get_width=5, width_units="'pixels'", width_min_pixels=3,
                     width_max_pixels=6, joint_rounded=True, cap_rounded=True,
                     pickable=True)


# A korábbi verzió világos CARTO-alaptérképe, OpenStreetMap-adatokkal.
OSM_STYLE = "light"

def basemap():
    st.pydeck_chart(pdk.Deck(layers=[], map_provider="carto", map_style=OSM_STYLE,
                            initial_view_state=pdk.ViewState(latitude=46.075, longitude=18.23, zoom=12)),
                    height=620, key="empty_map")


def default_osm():
    candidates = [ROOT / 'data/map.osm', ROOT / 'data/map',
                  ROOT.parent / 'pecs-geokodolo/map.osm', ROOT.parent / 'pecs-geokodolo/map']
    return next((str(p) for p in candidates if p.is_file()), '')


def clear_result():
    st.session_state.pop('result', None)
    for key in list(st.session_state):
        if key.startswith('filter_') or key == 'thresholds':
            del st.session_state[key]



def quality_panel(df, data):
    with st.expander("Adatminőség és kézi ellenőrzés", expanded=False):
        st.caption("A teljes betöltött állomány vizsgálata; a térképi szűrők nem módosítják ezt a jelentést. A jelzések nem törölnek rekordokat.")
        checked = inspect_data(df)
        counts = df.geocode_status.value_counts().rename_axis("Illesztés").reset_index(name="Hirdetések")
        counts["Arány (%)"] = (100 * counts.Hirdetések / len(df)).round(1)
        counts["Illesztés"] = counts["Illesztés"].replace({**LABELS, "unmatched": "Nem illesztett", "ambiguous": "Többértelmű"})
        st.dataframe(counts, hide_index=True)
        small = street_summary(data)
        small = small[small["count"] < 3]
        st.write(f"Kis mintájú utcák (1–2 hirdetés): {len(small)}; érintett hirdetések: {int(small['count'].sum())}.")
        st.dataframe(small[["street", "count", "median_price_m2"]].rename(columns={"street": "Utca", "count": "Hirdetések", "median_price_m2": "Medián kínálati ár / alapterület (Ft/m²)"}), hide_index=True)
        choices = {
            "Lehetséges többszörös hirdetések": "lehetseges_tobbszoros_hirdetes",
            "Nem magyarázott weboldali négyzetméterár": "magyarazatlan_areltérés",
            "Szélső négyzetméterár (IQR-szabály)": "szelso_negyzetmeterar",
            "Érvénytelen ár vagy alapterület": "ervenytelen_ar_vagy_terulet",
        }
        st.caption("Az egyező cím, ár és alapterület csak lehetséges többszörös hirdetésre utal. Az IQR-jelzés a teljes minta eloszlásán alapul; szélső ár önmagában nem hiba. A weboldali és a számított négyzetméterár eltérő területszámításból is adódhat.")
        selected = st.selectbox("Ellenőrzendő kategória", list(choices), key="quality_category")
        flagged = checked[checked[choices[selected]]]
        st.write(f"Ebben a kategóriában: {len(flagged)} hirdetés.")
        st.dataframe(flagged, hide_index=True)
        st.download_button("Kategória letöltése CSV-ben", csv_bytes(flagged), "adatminosegi_jelzesek.csv", "text/csv")
        sample = validation_sample(df)
        st.download_button("Kézi ellenőrzési minta letöltése", csv_bytes(sample), "kezi_ellenorzesi_minta.csv", "text/csv")
        st.caption("Legfeljebb 20 címillesztés és 20 utcaillesztés, rögzített véletlen mintával. A címillesztésnél a házszámot és az OSM-objektum helyét, utcaillesztésnél az utca nevét és vonalát ellenőrizd független referencia alapján. Az eredményhez írd be: helyes / hibás / nem eldönthető; add meg a forrást és megjegyzést. Egy utcaillesztés nem igazolja a lakás pontos helyét. A mintából számolt arány csak a vizsgált mintát jellemzi.")


def main():
    st.set_page_config(page_title="Pécs · Lakásárak", page_icon="🏘", layout="wide")
    st.title("Pécs · Lakásárak")
    with st.sidebar:
        st.header("1. Hirdetésadatok")
        uploaded = st.file_uploader("Nyers hirdetéskártya-CSV-k", type="csv", accept_multiple_files=True,
                                    help="Több oldalfájl vagy egy összevont CSV. Nem a korábbi listings_geocoded.csv.")
        st.header("2. OSM referencia")
        osm_upload = st.file_uploader("OSM XML-fájl (opcionális)", type=["osm", "xml"],
                                      help="Feltöltés helyett az alábbi helyi útvonal is használható.")
        osm_path = st.text_input("Helyi OSM-fájl útvonala", value=default_osm(),
                                 help="A meglévő map.osm vagy map fájl teljes útvonala. .pbf nem támogatott.")
        files = [(f.name, f.getvalue()) for f in uploaded]
        osm_bytes = osm_upload.getvalue() if osm_upload else None
        ready = bool(files) and (osm_bytes is not None or Path(osm_path).is_file())
        current = fingerprint(files, osm_bytes, osm_path if osm_bytes is None and Path(osm_path).is_file() else None)
        result = st.session_state.get('result')
        if result and result['fingerprint'] != current:
            clear_result()
            result = None
        if files:
            st.caption(f"{len(files)} CSV kiválasztva. A feldolgozás csak a gombbal indul.")
        if st.button("Geokódolás és elemzés indítása", type="primary", disabled=not ready):
            clear_result()
            bar = st.progress(0, text="Feldolgozás indítása…")
            try:
                result = process(files, osm_path or None, osm_bytes,
                                 progress=lambda n, label: bar.progress(n, text=label))
                result['fingerprint'] = current
                st.session_state['result'] = result
            except Exception as exc:
                result = None
                bar.empty()
                st.error(f"A feldolgozás nem sikerült: {exc}")
            else:
                bar.empty()
                st.success("A geokódolás és az utcai összesítés elkészült.")
        st.button("Eredmény törlése", on_click=clear_result, disabled=result is None)
    result = st.session_state.get('result')
    if result is None:
        basemap()
        return
    df = load_data(result['csv'])
    data = eligible(df)
    report = result['report']
    with st.expander("Feldolgozási jelentés és letöltések"):
        counts = report['geocode_status']
        st.write(f"Bemeneti sorok: {report['input_records']} · Elfogadott egyedi hirdetések: {len(df)} · Térképezhető: {len(data)}")
        st.write({"Címillesztés": counts.get('address_matched', 0), "Utcai közelítés": counts.get('street_approximate', 0),
                  "Nem illesztett": counts.get('unmatched', 0), "Többértelmű": counts.get('ambiguous', 0)})
        st.write("Beolvasási problémák / duplikátumok:", report['ingestion_issues'])
        st.caption("Azonos hirdetésazonosítónál az első érvényes rekord marad meg. Eltérő tartalom esetén a konfliktus külön szerepel a jelentésben.")
        st.download_button("Teljes eredménycsomag ZIP", result['zip'], "pecs_eredmenyek.zip", "application/zip")
        st.download_button("Geokódolt CSV", result['csv'], "listings_geocoded.csv", "text/csv")
        st.download_button("Ellenőrizendő hirdetések CSV", result['exports']['review.csv'], "review.csv", "text/csv")
    st.info(f"{len(df)} elfogadott hirdetésből {len(data)} térképezhető. A közelített helyek nem a lakások koordinátái. A minta nem reprezentálja Pécs piacát.")
    quality_panel(df, data)
    if data.empty:
        basemap()
        st.warning("Nincs illesztett, térképezhető hirdetés. Az eredménycsomagban ellenőrizhetők a kimaradások.")
        return
    with st.sidebar:
        st.header("3. Négyzetméterár-jelkulcs")
        lo, hi = int(np.floor(data.price_m2.min())), int(np.ceil(data.price_m2.max()))
        if lo < hi:
            thresholds = st.slider("Zöld / sárga / piros határértékek (Ft/m²)", min_value=lo, max_value=hi,
                                    value=(lo + (hi-lo)//3, lo + 2*(hi-lo)//3), step=1, key="thresholds")
        else:
            thresholds = (lo, hi)
            st.caption("Minden hirdetés négyzetméterára azonos; nincs állítható tartomány.")
        st.caption(f"Zöld: < {thresholds[0]:,} · Sárga: {thresholds[0]:,}–< {thresholds[1]:,} · Piros: ≥ {thresholds[1]:,} Ft/m²")
    with st.sidebar:
        statuses = st.multiselect("Illesztés", list(LABELS.values()), default=list(LABELS.values()), key="filter_statuses")
        streets = st.multiselect("Utcák (üresen: mind)", sorted(data.street.dropna().unique()), key="filter_streets")
        pmin = st.number_input("Minimum ár (millió Ft)", min_value=0.0, value=0.0, step=5.0, key="filter_pmin")
        pmax = st.number_input("Maximum ár (millió Ft)", min_value=0.0, value=float(np.ceil(data.price_million.max())), step=5.0, key="filter_pmax")
        amin = st.number_input("Minimum alapterület (m²)", min_value=0.0, value=0.0, step=5.0, key="filter_amin")
        amax = st.number_input("Maximum alapterület (m²)", min_value=0.0, value=float(np.ceil(data.area_m2.max())), step=5.0, key="filter_amax")
    shown = data[data.pontossag.isin(statuses) & data.price_million.between(pmin, pmax) & data.area_m2.between(amin, amax)].copy()
    if streets:
        shown = shown[shown.street.isin(streets)]
    if shown.empty:
        st.warning("A szűrőknek nincs megfelelő hirdetés. Módosítsd az ár-, terület- vagy pontossági szűrést."); basemap(); return
    summary = street_summary(shown, thresholds)
    with st.sidebar:
        min_count = st.number_input("Minimum hirdetésszám utcánként", min_value=1, value=1, step=1,
                                    key="filter_count", help="A többi szűrő után megmaradt hirdetések száma. Minden nézetre és a letöltésre érvényes.")
    summary = summary[summary["count"] >= min_count].copy()
    keys = pd.MultiIndex.from_frame(summary[["city", "street"]])
    shown = shown[pd.MultiIndex.from_frame(shown[["city", "street"]]).isin(keys)].copy()
    if shown.empty:
        st.warning("Nincs a minimum hirdetésszámnak megfelelő utca. Csökkentsd a küszöböt vagy módosítsd a többi szűrőt."); basemap(); return
    cols = st.columns(4)
    cols[0].metric("Hirdetések", len(shown))
    cols[1].metric("Utcák", len(summary))
    cols[2].metric("Medián ár", f"{shown.price_million.median():.1f} M Ft")
    cols[3].metric("Medián kínálati ár / alapterület", f"{shown.price_m2.median():,.0f} Ft/m²")
    map_tab, list_tab, method_tab = st.tabs(["Térkép", "Hirdetések", "Módszertan"])
    with map_tab:
        mode = st.radio("Megjelenítés", ["Utcánkénti összesítés", "Csak cím alapján illesztett hirdetések"], horizontal=True, key="filter_mode")
        if mode == "Utcánkénti összesítés":
            try:
                geometry = result['geometry']
                paths, missing = street_paths(summary, geometry)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                st.error(f"Nem olvashatók az utcavonalak: {exc}"); return
            layer = street_layer(paths)
            tooltip = {"html": "<b>{street_safe}</b><br>{median_label}<br>{count} hirdetés<br>{sample_label}<br>Címillesztés: {address_count}; közelítés: {approx_count}"}
            st.caption("Halvány vonal: 1–2 hirdetésből számolt medián. A hirdetésszám nem azonos a bizonyossággal. Az azonos utcához tartozó összes rendelkezésre álló vonalszakasz azonos színt kap. Az OSM-kivágat és a megszakadó geometriák korlátozhatják a lefedettséget.")
            if missing:
                st.warning("Vonalgeometria nélkül, csak a táblázatban: " + ', '.join(missing))
            center = summary[~summary.street.isin(missing)]
        else:
            center = shown[shown.geocode_status == "address_matched"].copy()
            center["address_safe"] = center.address_raw.map(html.escape)
            layer = pdk.Layer("ScatterplotLayer", center, get_position="[longitude, latitude]", get_fill_color=[35, 104, 173, 210], get_radius=20, radius_min_pixels=6, pickable=True)
            tooltip = {"html": "<b>{address_safe}</b><br>Hirdetés: {listing_id}<br>{price_million} M Ft · {area_m2} m²"}
            st.caption("OSM-címillesztések, kézi helyszínellenőrzés nélkül. Az egyező helyű pontok átfedhetnek; minden rekord a táblázatban elérhető.")
        if center.empty:
            st.warning("Ezekkel a szűrőkkel nincs megjeleníthető geometria.")
        else:
            st.pydeck_chart(pdk.Deck(layers=[layer], initial_view_state=pdk.ViewState(latitude=float(center.latitude.median()), longitude=float(center.longitude.median()), zoom=12), map_provider="carto", map_style=OSM_STYLE, tooltip=tooltip), height=560)
        st.subheader("Utcánkénti összesítés")
        st.dataframe(summary[["street", "count", "median_price_m2", "address_count", "approx_count"]].rename(columns={"street":"Utca", "count":"Hirdetések", "median_price_m2":"Medián Ft/m²", "address_count":"Címillesztés", "approx_count":"Közelítés"}), hide_index=True)
    with list_tab:
        st.dataframe(shown[["listing_id", "address_raw", "price_million", "area_m2", "price_m2", "pontossag"]], hide_index=True)
        st.download_button("Szűrt hirdetések letöltése CSV-ben", shown.to_csv(index=False, sep=";").encode("utf-8-sig"), file_name="szurt_hirdetesek.csv", mime="text/csv")
        st.scatter_chart(shown, x="area_m2", y="price_million", color="pontossag", x_label="Alapterület (m²)", y_label="Kínálati ár (M Ft)")
    with method_tab:
        st.markdown("""A négyzetméterár a kínálati ár és az alapterület hányadosa; az erkélyt nem számítjuk bele. A szűrés minden összesítést újraszámít. A minimum utcai hirdetésszámot az ár-, terület-, utca- és pontossági szűrés után ellenőrizzük; az így kizárt utcák hirdetései a mutatókban, táblázatokban és letöltésben sem szerepelnek. Az 1–2 hirdetéses utcák halványabbak. A 3-as küszöb szemléltetési szabály, nem statisztikai megbízhatósági határ.

A pontossági szűrés a hirdetéseket választja ki. Az utcánkénti térképen az eredmény összesített utcaszintű adat marad. Az ár alapján az utca vonalait színezzük, nem egy szemléltető pontot. Egy hirdetés esetén annak négyzetméterára adja az utca színét.

A vonalak a meglévő illesztések 150 méteres környezetében azonos nevű OSM-utakból indulnak, majd az összekapcsolódó, azonos nevű szakaszokat követik. A kivágat határa, hiányzó vagy nem kapcsolódó szakaszok miatt nem garantálható minden utca teljes hossza. Ez nem módosítja a hirdetések illesztési státuszát.

Az egyetlen hirdetésből képzett medián nem egy utca becsült árszintje. A különböző hirdetésazonosítók ugyanazt a lakást is takarhatják. A térképezésből kimaradt rekordok miatt szelekciós torzítás is lehetséges. Ezekből az adatokból ingatlanszintű hőtérkép, távolságelemzés és árbecslés nem indokolt.

Alaptérkép: CARTO világos stílus, OpenStreetMap-adatokkal. Az alaptérkép internetkapcsolatot igényel. A mintavétel időpontját az adatgyűjtés dokumentációjában kell rögzíteni.""")


if __name__ == "__main__":
    main()
