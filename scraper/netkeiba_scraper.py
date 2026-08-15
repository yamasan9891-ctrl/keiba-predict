"""
netkeiba スクレイパー

取得する情報:
  - レース結果ページ (db.netkeiba.com/race/<race_id>/)
    → 学習データ用（着順・タイム・オッズなどラベル付き）
  - 出馬表ページ (race.netkeiba.com/race/shutuba.html?race_id=<race_id>)
    → 予想対象レース用（まだ着順が出ていないレース）
  - 馬の過去成績ページ (db.netkeiba.com/horse/<horse_id>/)
    → 特徴量作成用の過去走データ

注意:
  - netkeibaのHTML構造は変わることがあります。動かない場合は対象ページを
    ブラウザで開き、開発者ツールでテーブルのクラス名等を確認して
    下記 SELECTORS を更新してください。
  - robots.txt と利用規約を確認し、SCRAPE_INTERVAL_SEC 以上の間隔を守ってください。
  - このファイルはネットワークアクセスを行うため、Claudeの実行環境からは
    テストできていません（サンドボックスのネットワークが制限されているため）。
    お使いの環境で実行前に、まず1レースだけで動作確認してください。
"""
import argparse
import json
import time
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

import sys
sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import DATA_RAW_DIR, REQUEST_HEADERS, SCRAPE_INTERVAL_SEC, RACE_ID_HELP

# HTML構造が変わったらここを直す
SELECTORS = {
    "result_table_class": "race_table_01",
    "shutuba_table_class": "Shutuba_Table",
    "horse_result_table_class": "db_h_race_results",
}

_session = requests.Session()
_session.headers.update(REQUEST_HEADERS)
_last_request_time = 0.0


def _polite_get(url: str) -> str:
    """アクセス間隔を空けつつGETする"""
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < SCRAPE_INTERVAL_SEC:
        time.sleep(SCRAPE_INTERVAL_SEC - elapsed)
    resp = _session.get(url, timeout=15)
    _last_request_time = time.time()
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    return resp.text


def fetch_race_result(race_id: str) -> pd.DataFrame:
    """
    確定済みレースの結果テーブルを取得する（学習データ用）。
    返すDataFrameの列（想定）:
      着順, 枠番, 馬番, 馬名, 性齢, 斤量, 騎手, タイム, 着差, 単勝, 人気, 馬体重, 調教師, horse_id, jockey_id
    """
    url = f"https://db.netkeiba.com/race/{race_id}/"
    html = _polite_get(url)
    soup = BeautifulSoup(html, "lxml")

    table = soup.find("table", class_=SELECTORS["result_table_class"])
    if table is None:
        raise RuntimeError(
            f"結果テーブルが見つかりません。SELECTORS['result_table_class'] を "
            f"{url} の実際のHTMLに合わせて修正してください。"
        )

    df = pd.read_html(str(table))[0]
    df.columns = [str(c).strip() for c in df.columns]

    # 馬・騎手のID抽出（詳細ページへのリンクから）
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
    return df


def fetch_shutuba(race_id: str) -> pd.DataFrame:
    """
    未確定レース（出馬表）を取得する（予想対象用）。
    着順・タイムはまだ存在しないため含まれない。
    """
    url = f"https://race.netkeiba.com/race/shutuba.html?race_id={race_id}"
    html = _polite_get(url)
    soup = BeautifulSoup(html, "lxml")

    table = soup.find("table", class_=SELECTORS["shutuba_table_class"])
    if table is None:
        raise RuntimeError(
            f"出馬表テーブルが見つかりません。SELECTORS['shutuba_table_class'] を "
            f"{url} の実際のHTMLに合わせて修正してください。"
        )

    df = pd.read_html(str(table))[0]
    # netkeibaの出馬表はヘッダーが複数行のことがあるため列名を平坦化
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
    """指定した馬の直近 n_races 走分の成績を取得"""
    url = f"https://db.netkeiba.com/horse/{horse_id}/"
    html = _polite_get(url)
    soup = BeautifulSoup(html, "lxml")

    table = soup.find("table", class_=SELECTORS["horse_result_table_class"])
    if table is None:
        return pd.DataFrame()

    df = pd.read_html(str(table))[0]
    df["horse_id"] = horse_id
    return df.head(n_races)


def _extract_id(href: str) -> str:
    """/horse/2019104567/ のようなURLから数字IDを抜き出す"""
    parts = [p for p in href.split("/") if p]
    return parts[-1] if parts else None


def save_raw(df: pd.DataFrame, name: str, race_id: str) -> Path:
    out_path = DATA_RAW_DIR / f"{name}_{race_id}.csv"
    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    return out_path


def main():
    parser = argparse.ArgumentParser(description="netkeibaスクレイパー")
    parser.add_argument("--race-id", required=True, help=RACE_ID_HELP)
    parser.add_argument(
        "--mode", choices=["result", "shutuba"], default="shutuba",
        help="result: 確定済みレース結果（学習用） / shutuba: 出馬表（予想対象）",
    )
    parser.add_argument(
        "--with-horse-history", action="store_true",
        help="出走馬ごとの過去成績も併せて取得する",
    )
    args = parser.parse_args()

    if args.mode == "result":
        df = fetch_race_result(args.race_id)
    else:
        df = fetch_shutuba(args.race_id)

    out_path = save_raw(df, args.mode, args.race_id)
    print(f"保存しました: {out_path} ({len(df)}行)")

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
