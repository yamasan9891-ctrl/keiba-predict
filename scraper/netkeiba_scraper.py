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
    # db.netkeiba.com は EUC-JP 固定（自動判定(apparent_encoding)が不安定で文字化けすることがあるため）。
    # race.netkeiba.com は UTF-8 系のため従来通り自動判定に任せる。
    if "db.netkeiba.com" in url:
        resp.encoding = "euc-jp"
    else:
        resp.encoding = resp.apparent_encoding
    return resp.text


def _extract_meta_from_soup(race_id: str, soup: BeautifulSoup) -> dict:
    """soupから各種メタ情報を抽出する共通ロジック（race.netkeiba.com / db.netkeiba.com どちらにも使う）"""
    race_name_el = soup.find("div", class_=SELECTORS["race_name_class"]) or soup.find(class_=SELECTORS["race_name_class"])
    race_data01 = soup.find("div", class_=SELECTORS["race_data_class"]) or soup.find(class_=SELECTORS["race_data_class"])
    race_data02 = soup.find("div", class_=SELECTORS["race_data02_class"]) or soup.find(class_=SELECTORS["race_data02_class"])

    race_name = race_name_el.get_text(strip=True) if race_name_el else None
    data01_text = race_data01.get_text(" ", strip=True) if race_data01 else ""
    data02_text = race_data02.get_text(" ", strip=True) if race_data02 else ""
    full_text = f"{data01_text} {data02_text}"

    # db.netkeiba.com側は開催日が別の場所（smalltxt等）に入っていることがあるため、
    # ページ全体のテキストも保険として日付検索の対象に含める
    full_text_for_date = full_text
    if not re.search(r"\d{1,2}月\d{1,2}日", full_text_for_date):
        page_text = soup.get_text(" ", strip=True)
        full_text_for_date = full_text + " " + page_text[:500]

    surface_match = re.search(r"(芝|ダート|ダ|障害)", data01_text)
    if surface_match and surface_match.group(1) == "ダ":
        surface_match_value = "ダート"  # 出馬表ページでは「ダ」と略記されるため正式名称に統一
    elif surface_match:
        surface_match_value = surface_match.group(1)
    else:
        surface_match_value = None
    distance_match = re.search(r"(\d{3,4})m", data01_text)
    # 出馬表ページでは「稍重」→「稍」、「不良」→「不」のように1文字に省略されることがあるため両対応
    condition_match = re.search(r"馬場[:：]?\s*(稍重|不良|良|重|稍|不)", full_text)
    _condition_normalize = {"稍": "稍重", "不": "不良"}
    condition_value = None
    if condition_match:
        raw = condition_match.group(1)
        condition_value = _condition_normalize.get(raw, raw)
    weather_match = re.search(r"天候[:：]?\s*(晴|曇|雨|小雨|雪)", full_text)
    grade_match = re.search(r"(G1|G2|G3|GI|GII|GIII|Ｇ1|Ｇ2|Ｇ3|OP|オープン|L)", (race_name or "") + full_text)
    is_handicap = 1 if ("ハンデ" in full_text or "ハンデ" in (race_name or "")) else 0

    date_match = re.search(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日", full_text_for_date)
    race_date = None
    if date_match:
        year_str = date_match.group(1) or race_id[:4]
        month, day = int(date_match.group(2)), int(date_match.group(3))
        race_date = f"{year_str}-{month:02d}-{day:02d}"

    course_match = re.search(r"(\d+)回\s*(東京|中山|阪神|京都|中京|新潟|福島|小倉|札幌|函館)\s*(\d+)日", full_text_for_date)
    course = course_match.group(2) if course_match else None
    meeting_label = f"{course_match.group(1)}回{course_match.group(2)}{course_match.group(3)}日目" if course_match else course
    race_number = None

    # 発走時刻（例: "15:30発走"）
    post_time_match = re.search(r"(\d{1,2}:\d{2})\s*発走", full_text)
    post_time = post_time_match.group(1) if post_time_match else None

    # <title>タグに「2026年8月29日 新潟1R」のような、日付・開催場・R番号が
    # 揃った信頼できる表記があるため、これを最優先で使う（RaceData01/02の
    # 改行・省略表記のばらつきに影響されないため）
    title_text = soup.title.get_text() if soup.title else ""
    title_match = re.search(
        r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*(東京|中山|阪神|京都|中京|新潟|福島|小倉|札幌|函館)(\d{1,2})R",
        title_text,
    )
    if title_match:
        y, mo, d, c, rn = title_match.groups()
        race_date = f"{y}-{int(mo):02d}-{int(d):02d}"
        course = c
        race_number = int(rn)  # 文字列のままだと"10"<"2"のような順序バグになるためint化

    return {
        "race_id": race_id,
        "race_date": race_date,
        "course": course,
        "meeting_label": meeting_label,
        "post_time": post_time,
        "race_number": race_number,
        "distance": int(distance_match.group(1)) if distance_match else None,
        "surface": surface_match_value,
        "track_condition": condition_value,
        "weather": weather_match.group(1) if weather_match else None,
        "is_handicap": is_handicap,
        "race_name": race_name,
        "grade": grade_match.group(1) if grade_match else None,
        "is_win5_race": 0,
    }


def fetch_race_meta(race_id: str, soup: BeautifulSoup = None) -> dict:
    """
    レースのメタ情報を取得する。soup未指定の場合、
    まずrace.netkeiba.com（直近レース向け）を試し、開催日が取れなければ
    db.netkeiba.com（過去レースのアーカイブ、通常こちらの方が古いレースの情報が充実）を試す。
    """
    if soup is not None:
        return _extract_meta_from_soup(race_id, soup)

    url = f"https://race.netkeiba.com/race/result.html?race_id={race_id}&rf=race_list"
    html = _polite_get(url)
    soup1 = BeautifulSoup(html, "html.parser")
    meta = _extract_meta_from_soup(race_id, soup1)

    if meta.get("race_date") is None:
        try:
            url2 = f"https://db.netkeiba.com/race/{race_id}/"
            html2 = _polite_get(url2)
            soup2 = BeautifulSoup(html2, "lxml")
            meta2 = _extract_meta_from_soup(race_id, soup2)
            # db側で取れた項目があれば、race.netkeiba.com側の結果を補完する
            for key, value in meta2.items():
                if value is not None and meta.get(key) is None:
                    meta[key] = value
        except Exception:
            pass

    return meta


def _find_result_table(soup: BeautifulSoup):
    table = soup.find("table", attrs={"summary": "レース結果"})
    if table is not None:
        return table
    for cls in ("RaceTable01", "race_table_01"):
        table = soup.find("table", class_=cls)
        if table is not None:
            return table
    return None


def _looks_like_valid_result_table(df: pd.DataFrame) -> bool:
    if df is None or len(df) < 3:
        return False
    cols = [str(c) for c in df.columns]
    return any(
        ("着" in c and "順" in c) or ("馬" in c and "番" in c) or c == "馬名"
        for c in cols
    )


def fetch_race_result(race_id: str) -> pd.DataFrame:
    url = f"https://race.netkeiba.com/race/result.html?race_id={race_id}&rf=race_list"
    html = _polite_get(url)
    soup = BeautifulSoup(html, "html.parser")

    table = _find_result_table(soup)
    df = None
    if table is not None:
        df = pd.read_html(io.StringIO(str(table)))[0]
        df.columns = [str(c).strip() for c in df.columns]

    if not _looks_like_valid_result_table(df):
        url2 = f"https://db.netkeiba.com/race/{race_id}/"
        html = _polite_get(url2)
        soup = BeautifulSoup(html, "lxml")
        table = _find_result_table(soup)
        if table is not None:
            df = pd.read_html(io.StringIO(str(table)))[0]
            df.columns = [str(c).strip() for c in df.columns]
        if not _looks_like_valid_result_table(df):
            raise RuntimeError(f"有効な結果テーブルが見つかりません（存在しないレースの可能性）: {race_id}")

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

    # 以前はここでhorse_ids/jockey_idsを集めるだけでdfに反映し忘れていたバグを修正
    if len(horse_ids) == len(df):
        df["horse_id"] = horse_ids
        df["jockey_id"] = jockey_ids

    df["race_id"] = race_id
    df.attrs["meta"] = fetch_race_meta(race_id, soup)
    return df


def fetch_race_ids_for_date(date_str: str) -> list:
    """
    指定日（YYYYMMDD形式）に開催される全レースのrace_idを取得する。
    race.netkeiba.com/top/race_list.html は表示中にJavaScriptでレース一覧を
    後から読み込む形式のため、そのページが内部で呼び出している
    race_list_sub.html（静的HTMLを返す）を直接使う
    （実ブラウザでのネットワーク調査により確認済み）。
    """
    url = f"https://race.netkeiba.com/top/race_list_sub.html?kaisai_date={date_str}"
    html = _polite_get(url)
    soup = BeautifulSoup(html, "html.parser")

    race_ids = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        match = re.search(r"race_id=(\d{12})", href)
        if match:
            race_ids.add(match.group(1))

    return sorted(race_ids)


def get_upcoming_weekend_dates(reference_date=None) -> list:
    """
    基準日から見て、直近の土曜・日曜の日付（YYYYMMDD形式）を返す。
    基準日自体が既に土曜・日曜の場合は、その日（今週末）を対象にする
    （来週末までスキップしてしまわないようにするため）。
    JRAは基本的に土日開催（一部金曜ナイター等の例外はここでは考慮しない）。
    """
    import datetime as _dt
    ref = reference_date or _dt.date.today()
    weekday = ref.weekday()  # 月曜=0 ... 土曜=5, 日曜=6

    if weekday == 5:  # 今日が土曜日 → 今日と明日（日曜）
        saturday = ref
    elif weekday == 6:  # 今日が日曜日 → 今日の土日（土曜は昨日）
        saturday = ref - _dt.timedelta(days=1)
    else:  # 平日 → 次の土曜日を探す
        days_until_saturday = (5 - weekday) % 7
        saturday = ref + _dt.timedelta(days=days_until_saturday)

    sunday = saturday + _dt.timedelta(days=1)
    return [saturday.strftime("%Y%m%d"), sunday.strftime("%Y%m%d")]


def fetch_this_week_race_ids() -> list:
    """今週末（土日）に開催される全レースのrace_idをまとめて取得する"""
    all_ids = []
    for date_str in get_upcoming_weekend_dates():
        try:
            ids = fetch_race_ids_for_date(date_str)
            print(f"  {date_str}: {len(ids)}レース")
            all_ids.extend(ids)
        except Exception as e:
            print(f"  [警告] {date_str} の取得に失敗: {e}")
    return all_ids


JRA_COURSE_CODES = {
    "01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京",
    "06": "中山", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉",
}


def fetch_next_week_preview() -> dict:
    """
    来週末（今週末の7日後）の開催予定を軽量に取得する。
    個々のレース情報は取得せず、race_idに埋め込まれた競馬場コードから
    開催場名だけを割り出す（予想はまだ行わない、日程の先出し表示専用）。
    """
    import datetime as _dt
    this_weekend = get_upcoming_weekend_dates()
    next_dates = [
        (_dt.datetime.strptime(d, "%Y%m%d").date() + _dt.timedelta(days=7)).strftime("%Y%m%d")
        for d in this_weekend
    ]

    courses_by_date = {}
    for date_str in next_dates:
        try:
            ids = fetch_race_ids_for_date(date_str)
        except Exception as e:
            print(f"[警告] 来週プレビュー取得失敗 {date_str}: {e}")
            ids = []
        codes = {rid[4:6] for rid in ids}
        names = sorted({JRA_COURSE_CODES.get(c, c) for c in codes})
        courses_by_date[date_str] = names

    return {"dates": next_dates, "courses_by_date": courses_by_date}


def fetch_win5_race_ids() -> set:
    """
    今週のWIN5対象レース（5レース）のrace_idを取得する。
    race.netkeiba.com/top/win5.html は常に「今週」のWIN5対象を返す。
    """
    try:
        url = "https://race.netkeiba.com/top/win5.html"
        html = _polite_get(url)
        ids = set(re.findall(r"race_id=(\d{12})", html))
        return ids
    except Exception as e:
        print(f"[警告] WIN5対象レースの取得に失敗: {e}")
        return set()


def fetch_horse_past_results(horse_id: str, n_races: int = 10) -> pd.DataFrame:
    """指定した馬の直近 n_races 走分の成績を取得する"""
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
    if df is None or df.empty:
        print("[警告] save_to_db: 空のDataFrameが渡されたため何もしません")
        return
    race_id = str(df["race_id"].iloc[0])
    meta = meta or df.attrs.get("meta") or {"race_id": race_id}
    meta["race_id"] = race_id

    rename = {
        "着 順": "finish_pos", "枠": "post_position", "馬 番": "horse_number",
        "斤量": "weight_carried", "単勝 オッズ": "win_odds", "人 気": "popularity",
        "馬体重 (増減)": "horse_weight_raw", "後3F": "last_3f", "コーナー 通過順": "passing",
        "馬名": "horse_name", "タイム": "finish_time_raw", "厩舎": "trainer_name",
        "着順": "finish_pos", "枠番": "post_position", "馬番": "horse_number",
        "単勝": "win_odds", "人気": "popularity", "馬体重": "horse_weight_raw",
        "上がり": "last_3f", "通過": "passing",
    }
    d = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

    def _parse_finish_time(raw) -> float:
        """'1:33.4'（1分33秒4）のような表記を秒数(93.4)に変換する"""
        if raw is None or (isinstance(raw, float) and pd.isna(raw)):
            return None
        s = str(raw).strip()
        m = re.match(r"(?:(\d+):)?(\d+)\.(\d+)", s)
        if not m:
            return None
        minutes = int(m.group(1)) if m.group(1) else 0
        seconds = int(m.group(2))
        frac = int(m.group(3))
        frac_digits = len(m.group(3))
        return minutes * 60 + seconds + frac / (10 ** frac_digits)

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
            "trainer_name": r.get("trainer_name"),
            "weight_carried": float(r["weight_carried"]) if pd.notna(r.get("weight_carried")) else None,
            "horse_weight": float(w_match.group(1)) if w_match else None,
            "horse_weight_diff": float(wd_match.group(1)) if wd_match else None,
            "running_style": infer_running_style(r.get("passing"), n_horses) if "passing" in d.columns else None,
            "last_3f": float(r["last_3f"]) if pd.notna(r.get("last_3f")) else None,
            "finish_time": _parse_finish_time(r.get("finish_time_raw")),
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
    