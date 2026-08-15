"""
オッズスクレイパー（馬連・馬単・3連複・3連単）

netkeibaのオッズページ（race.netkeiba.com/odds/index.html?type=...&race_id=...）から
各馬券種のオッズを取得する。ページ構成はJSで描画される部分もあり、単純な requests+BeautifulSoup
では取得できないことがある点に注意（その場合はSelenium等のブラウザ自動化が別途必要）。

まずは以下を試し、取得できなければ selenium 版に切り替えてください
（このファイルはネットワーク制限のある環境で書いているため未検証です）。

各関数は {horse_or_tuple: odds(float)} の辞書を返す。
"""
import re
from pathlib import Path

import requests
from bs4 import BeautifulSoup

import sys
sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import REQUEST_HEADERS, SCRAPE_INTERVAL_SEC
import time

_session = requests.Session()
_session.headers.update(REQUEST_HEADERS)
_last = 0.0


def _get(url):
    global _last
    elapsed = time.time() - _last
    if elapsed < SCRAPE_INTERVAL_SEC:
        time.sleep(SCRAPE_INTERVAL_SEC - elapsed)
    r = _session.get(url, timeout=15)
    _last = time.time()
    r.raise_for_status()
    r.encoding = r.apparent_encoding
    return r.text


# netkeibaのodds typeパラメータ（変更されている可能性があるため要確認）
ODDS_TYPES = {
    "umaren": 4,
    "umatan": 5,
    "wide": 7,
    "sanrenpuku": 8,
    "sanrentan": 6,
}


def fetch_odds(race_id: str, bet_type: str) -> dict:
    """
    bet_type: 'umaren' | 'umatan' | 'sanrenpuku' | 'sanrentan'
    戻り値の例:
      umaren     -> {frozenset({'1','5'}): 6.2, ...}
      umatan     -> {('1','5'): 12.3, ...}
      sanrenpuku -> {frozenset({'1','5','8'}): 25.4, ...}
      sanrentan  -> {('1','5','8'): 120.5, ...}
    馬番を文字列のまま使う（features側で馬番→馬名変換して表示する）
    """
    if bet_type not in ODDS_TYPES:
        raise ValueError(f"未対応のbet_type: {bet_type}")

    type_num = ODDS_TYPES[bet_type]
    url = f"https://race.netkeiba.com/odds/index.html?type={type_num}&race_id={race_id}"
    html = _get(url)
    soup = BeautifulSoup(html, "lxml")

    result = {}
    tables = soup.find_all("table")
    for table in tables:
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            text_cells = [c.get_text(strip=True) for c in cells]
            numbers_in_row = [c for c in text_cells if re.fullmatch(r"\d+", c)]
            odds_cells = [c for c in text_cells if re.fullmatch(r"\d+\.\d", c)]
            if not odds_cells:
                continue
            odds_val = float(odds_cells[-1])

            if bet_type == "umaren" and len(numbers_in_row) >= 2:
                key = frozenset(numbers_in_row[:2])
                result[key] = odds_val
            elif bet_type == "umatan" and len(numbers_in_row) >= 2:
                key = (numbers_in_row[0], numbers_in_row[1])
                result[key] = odds_val
            elif bet_type == "sanrenpuku" and len(numbers_in_row) >= 3:
                key = frozenset(numbers_in_row[:3])
                result[key] = odds_val
            elif bet_type == "sanrentan" and len(numbers_in_row) >= 3:
                key = (numbers_in_row[0], numbers_in_row[1], numbers_in_row[2])
                result[key] = odds_val

    if not result:
        print(
            f"[警告] {bet_type} のオッズが1件も取得できませんでした。"
            f"netkeibaのページ構造（JS描画など）に合わせてこの関数の実装を見直してください: {url}"
        )
    return result


def fetch_all_odds(race_id: str) -> dict:
    return {bt: fetch_odds(race_id, bt) for bt in ODDS_TYPES}
