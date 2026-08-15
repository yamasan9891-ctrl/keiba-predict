"""
netkeiba スクレイパー
"""
import argparse
import io
import re
import time
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

import sys
sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import DATA_RAW_DIR, REQUEST_HEADERS, SCRAPE_INTERVAL_SEC, RACE_ID_HELP
from db.database import get_conn, init_db, upsert_race, upsert_entries

SELECTORS = {
    "shutuba_table_class": "Shutuba_Table",
    "horse_result_table_class": "db_h_race_results",
    "race_data_class": "RaceData01",
    "race_name_class": "RaceName",
    "race_data02_class": "RaceData02",
}

_session = requests.Session()
_session.headers.update(REQUEST_HEADERS)
_last_request_time = 0.0


def _polite_get(url: str) -> str:
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < SCRAPE_INTERVAL_SEC:
        time.sleep(SCRAPE_INTERVAL_SEC - elapsed)
    resp = _session.get(url, timeout=15)
    _last_request_time = time.time()
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    return resp.text


def fetch_race_meta(race_id: str, soup: BeautifulSoup = None) -> dict:
    if soup is None:
        url = f"https://race.netkeiba.com/race/result.html?race_id={race_id}&rf=race_list"
        html = _polite_get(url)
        soup = BeautifulSoup(html, "html.parser")

    race_name_el = soup.find("div", class_=SELECTORS["race_name_class"])
    race_data01 = soup.find("div", class_=SELECTORS["race_data_class"])
    race_data02 = soup.find("div", class_=SELECTORS["race_data02_class"])

    race_name = race_name_el.get_text(strip=True) if race_name_el else None
    data01_text = race_data01.get_text(" ", strip=True) if race_data01 else ""
    data02_text = race_data02.get_text(" ", strip=True) if race_data02 else ""
    full_text = f"{data01_text} {data02_text}"

    surface_match = re.search(r"(芝|ダート|障害)", data01_text)
    distance_match = re.search(r"(\d{3,4})m", data01_text)
    condition_match = re.search(r"馬場[:：]?\s*(良|稍重|重|不良)", full_text)
    weather_match = re.search(r"天候[:：]?\s*(晴|曇|雨|小雨|雪)", full_text)
    grade_match = re.search(r"(G1|G2|G3|GI|GII|GIII|Ｇ1|Ｇ2|Ｇ3|OP|オープン|L)", (race_name or "") + full_text)
    is_handicap = 1 if ("ハンデ" in full_text or "ハンデ" in (race_name or "")) else 0

    return {
        "race_id": race_id,
        "race_date": None,
        "course": None,
        "distance": int(distance_match.group(1)) if distance_match else None,
        "surface": surface_match.group(1) if surface_match else None,
        "track_condition": condition_match.group(1) if condition_match else None,
        "weather": weather_match.group(1) if weather_match else None,
        "is_handicap": is_handicap,
        "race_name": race_name,
        "grade": grade_match.group(1) if grade_match else None,
        "is_win5_race": 0,
    }


def _find_result_table(soup: BeautifulSoup):
    table = soup.find("table", attrs={"summary": "レース結果"})
    if table is not None:
        return table
    for cls in ("RaceTable01", "race_table_01"):
        table = soup.find("table", class_=cls)
        if table is not None:
            return table
    return soup.find("table")


def fetch_race_result(race_id: str) -> pd.DataFrame:
    url = f"https://race.netkeiba.com/race/result.html?race_id={race_id}&rf=race_list"
    html = _polite_get(url)
    soup = BeautifulSoup(html, "html.parser")

    table = _find_result_table(soup)
    if table is None:
        url2 = f"https://db.netkeiba.com/race/{race_id}/"
        html = _polite_get(url2)
        soup = BeautifulSoup(html, "lxml")
        table = _find_result_table(soup)
        if table is None:
            raise RuntimeError(f"結果テーブルが見つかりません: {url} / {url2}")

    df = pd.read_html(io.StringIO(str(table)))[0]
    df.columns = [str(c).strip() for c in df.columns]

    horse_ids, jockey_ids = [], []
    for row in table.find_all("tr")[1:]:
        horse_link = row.find("a", href=lambda h: h and "/horse/" in h)
        jockey_link = row.find("a", href=lambda h: h and "/jockey/" in h)
        horse_ids.append(_extract_id(horse_link["href"]) if horse_link else None)
        jockey_ids.append(_extract_id(jockey_link["href"]) if jockey_link else None)

    if len(horse_ids) == len(df):
        df["horse_id"] = horse_ids
        df["jockey_id"] = jockey_ids

    df["race_id"] = race_id
    df.attrs["meta"] = fetch_race_meta(race_id, soup)
    return df


def infer_running_style(passing_positions: str, n_horses: int) -> str:
    if not passing_positions or not isinstance(passing_positions, str):
        return None
    parts = re.findall(r"\d+", passing_positions)
    if not parts or n_horses == 0:
        return None
    first_corner = int(parts[0])
    ratio = first_corner / n_horses
    if ratio <= 0.15:
        return "逃げ"
    elif ratio <= 0.4:
        return "先行"
    elif ratio <= 0.7:
        return "差し"
    else:
        return "追込"


def fetch_shutuba(race_id: str) -> pd.DataFrame:
    url = f"https://race.netkeiba.com/race/shutuba.html?race_id={race_id}"
    html = _polite_get(url)
    soup = BeautifulSoup(html, "html.parser")

    table = soup.find("table", class_=SELECTORS["shutuba_table_class"])
    if table is None:
        raise RuntimeError(f"出馬表テーブルが見つかりません: {url}")

    df = pd.read_html(io.StringIO(str(table)))[0]
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = ["_".join(map(str, c)).strip() for c in df.columns]

    horse_ids, jockey_ids = [], []
    for row in table.find_all("tr"):
        horse_link = row.find("a", href=lambda h: h and "/horse/" in h)
        jockey_link = row.find("a", href=lambda h: h and "/jockey/" in h)
        if horse_link:
            horse_ids.append(_extract_id(horse_link["href"]))
            jockey_ids.append(_extract_id(jockey_link["href"]) if jockey_link else None)

    df["race_id"] = race_id
    return df


def fetch_horse_past_results(horse_id: str, n_races: int = 10) -> pd.DataFrame:
    url = f"https://db.netkeiba.com/horse/{horse_id}/"
    html = _polite_get(url)
    soup = BeautifulSoup(html, "lxml")

    table = soup.find("table", class_=SELECTORS["horse_result_table_class"])
    if table is None:
        return pd.DataFrame()

    df = pd.read_html(io.StringIO(str(table)))[0]
    df["horse_id"] = horse_id
    return df.head(n_races)


def _extract_id(href: str) -> str:
    parts = [p for p in href.split("/") if p]
    return parts[-1] if parts else None


def save_raw(df: pd.DataFrame, name: str, race_id: str) -> Path:
    out_path = DATA_RAW_DIR / f"{name}_{race_id}.csv"
    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    return out_path


def save_to_db(df: pd.DataFrame, meta: dict = None) -> None:
    init_db()
    race_id = str(df["race_id"].iloc[0])
    meta = meta or df.attrs.get("meta") or {"race_id": race_id}
    meta["race_id"] = race_id

    rename = {
        "着 順": "finish_pos", "枠": "post_position", "馬 番": "horse_number",
        "斤量": "weight_carried", "単勝 オッズ": "win_odds", "人 気": "popularity",
        "馬体重 (増減)": "horse_weight_raw", "後3F": "last_3f", "コーナー 通過順": "passing",
        "馬名": "horse_name",
        "着順": "finish_pos", "枠番": "post_position", "馬番": "horse_number",
        "単勝": "win_odds", "人気": "popularity", "馬体重": "horse_weight_raw",
        "上がり": "last_3f", "通過": "passing",
    }
    d = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

    entries = []
    n_horses = len(d)
    for _, r in d.iterrows():
        weight_raw = str(r.get("horse_weight_raw", ""))
        w_match = re.search(r"(\d+)", weight_raw)
        wd_match = re.search(r"\(([+\-]?\d+)\)", weight_raw)
        finish_pos = None
        if "finish_pos" in d.columns:
            fp = re.sub(r"[^0-9]", "", str(r.get("finish_pos", "")))
            finish_pos = int(fp) if fp else None

        entries.append({
            "race_id": race_id,
            "horse_number": int(r["horse_number"]) if pd.notna(r.get("horse_number")) else None,
            "post_position": int(r["post_position"]) if pd.notna(r.get("post_position")) else None,
            "horse_id": r.get("horse_id"),
            "horse_name": r.get("horse_name"),
            "jockey_id": r.get("jockey_id"),
            "jockey_name": None,
            "weight_carried": float(r["weight_carried"]) if pd.notna(r.get("weight_carried")) else None,
            "horse_weight": float(w_match.group(1)) if w_match else None,
            "horse_weight_diff": float(wd_match.group(1)) if wd_match else None,
            "running_style": infer_running_style(r.get("passing"), n_horses) if "passing" in d.columns else None,
            "last_3f": float(r["last_3f"]) if pd.notna(r.get("last_3f")) else None,
            "finish_pos": finish_pos,
            "win_odds": float(r["win_odds"]) if pd.notna(r.get("win_odds")) else None,
            "popularity": int(r["popularity"]) if pd.notna(r.get("popularity")) else None,
            "is_placed": (1 if finish_pos and finish_pos <= 3 else (0 if finish_pos else None)),
        })

    with get_conn() as conn:
        upsert_race(conn, meta)
        upsert_entries(conn, entries)


def main():
    parser = argparse.ArgumentParser(description="netkeibaスクレイパー")
    parser.add_argument("--race-id", required=True, help=RACE_ID_HELP)
    parser.add_argument("--mode", choices=["result", "shutuba"], default="shutuba")
    parser.add_argument("--with-horse-history", action="store_true")
    args = parser.parse_args()

    if args.mode == "result":
        df = fetch_race_result(args.race_id)
    else:
        df = fetch_shutuba(args.race_id)

    out_path = save_raw(df, args.mode, args.race_id)
    print(f"保存しました: {out_path} ({len(df)}行)")
    save_to_db(df)
    print("DBにも保存しました")

    if args.with_horse_history and "horse_id" in df.columns:
        histories = []
        for hid in df["horse_id"].dropna().unique():
            print(f"  馬ID {hid} の過去成績を取得中...")
            hist = fetch_horse_past_results(hid)
            if not hist.empty:
                histories.append(hist)
        if histories:
            hist_df = pd.concat(histories, ignore_index=True)
            hist_path = save_raw(hist_df, "horse_history", args.race_id)
            print(f"保存しました: {hist_path} ({len(hist_df)}行)")


if __name__ == "__main__":
    main()
    