"""
オッズスクレイパー（検証済み版）

race.netkeiba.com/api/api_get_jra_odds.html?race_id=X&type=N という
JSON APIから直接オッズを取得する（HTML内のテーブルはJSで後から埋まる
プレースホルダーのため、直接APIを叩く方式にしている）。

typeの値（実際のレースで検証済み）:
  4 = 馬連   (組み合わせ, 例: 16頭ならC(16,2)=120通り)
  6 = 馬単   (順列, 16頭なら16×15=240通り、順序あり)
  7 = 3連複  (組み合わせ, 16頭ならC(16,3)=560通り)
  8 = 3連単  (順列, 16頭なら16×15×14=3360通り、順序あり)
"""
import json
import time
from pathlib import Path

import requests

import sys
sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import REQUEST_HEADERS, SCRAPE_INTERVAL_SEC

_session = requests.Session()
_session.headers.update(REQUEST_HEADERS)
_last = 0.0

ODDS_TYPES = {
    "tan": 1,
    "umaren": 4,
    "umatan": 6,
    "sanrenpuku": 7,
    "sanrentan": 8,
}


def _get(url: str, params: dict) -> dict:
    global _last
    elapsed = time.time() - _last
    if elapsed < SCRAPE_INTERVAL_SEC:
        time.sleep(SCRAPE_INTERVAL_SEC - elapsed)
    r = _session.get(url, params=params, timeout=15)
    _last = time.time()
    r.raise_for_status()
    return json.loads(r.text)


def _parse_odds_value(raw) -> float:
    """'1,027.8' のようなカンマ区切り文字列をfloatに変換する"""
    try:
        return float(str(raw).replace(",", ""))
    except (ValueError, TypeError):
        return None


def fetch_odds(race_id: str, bet_type: str) -> dict:
    """
    bet_type: 'tan' | 'umaren' | 'umatan' | 'sanrenpuku' | 'sanrentan'
    戻り値の例:
      tan        -> {'1': 3.5, ...}           # 単勝オッズ（馬番→オッズ）
      umaren     -> {frozenset({'1','5'}): 6.2, ...}
      umatan     -> {('1','5'): 12.3, ...}   # 1着→2着の順
      sanrenpuku -> {frozenset({'1','5','8'}): 25.4, ...}
      sanrentan  -> {('1','5','8'): 120.5, ...}  # 1着→2着→3着の順
    馬番は先頭ゼロを外した文字列（'01'ではなく'1'）で統一する。
    """
    if bet_type not in ODDS_TYPES:
        raise ValueError(f"未対応のbet_type: {bet_type}")

    type_num = ODDS_TYPES[bet_type]
    url = "https://race.netkeiba.com/api/api_get_jra_odds.html"
    data = _get(url, {"race_id": race_id, "type": type_num})

    odds_root = data.get("data", {}).get("odds", {})
    if not odds_root:
        print(f"[警告] {bet_type} のオッズが空でした（発売前 or race_id不正の可能性）: {race_id}")
        return {}

    inner = odds_root.get(str(type_num)) or odds_root.get(type_num)
    if inner is None:
        for v in odds_root.values():
            if isinstance(v, dict):
                inner = v
                break
    if inner is None:
        return {}

    result = {}
    for key, value in inner.items():
        odds_val = _parse_odds_value(value[0] if isinstance(value, list) else value)
        if odds_val is None:
            continue

        if bet_type == "tan":
            # keyは2桁の馬番そのもの（例: "01" = 1番）
            horse_num = str(int(key))
            result[horse_num] = odds_val
            continue

        # 2桁ごとの馬番連結（例: "0102" = 1番→2番, "010203" = 1番→2番→3番）
        digits = [key[i:i+2] for i in range(0, len(key), 2)]
        horse_nums = [str(int(d)) for d in digits]

        if bet_type == "umaren":
            result[frozenset(horse_nums[:2])] = odds_val
        elif bet_type == "umatan":
            result[tuple(horse_nums[:2])] = odds_val
        elif bet_type == "sanrenpuku":
            result[frozenset(horse_nums[:3])] = odds_val
        elif bet_type == "sanrentan":
            result[tuple(horse_nums[:3])] = odds_val

    return result


def fetch_tan_odds_and_popularity(race_id: str) -> dict:
    """
    単勝オッズと人気順位を同時に取得する（type=1のレスポンスは
    [オッズ, 複勝下限（未使用）, 人気順位] という並びのため）。
    戻り値: {horse_number_str: {"odds": float, "popularity": int}}
    """
    url = "https://race.netkeiba.com/api/api_get_jra_odds.html"
    data = _get(url, {"race_id": race_id, "type": ODDS_TYPES["tan"]})
    odds_root = data.get("data", {}).get("odds", {})
    inner = odds_root.get(str(ODDS_TYPES["tan"])) or odds_root.get(ODDS_TYPES["tan"]) or {}

    result = {}
    for key, value in inner.items():
        if not isinstance(value, list) or len(value) < 3:
            continue
        horse_num = str(int(key))
        odds_val = _parse_odds_value(value[0])
        try:
            popularity = int(value[2])
        except (ValueError, TypeError):
            popularity = None
        result[horse_num] = {"odds": odds_val, "popularity": popularity}
    return result


def fetch_all_odds(race_id: str) -> dict:
    return {bt: fetch_odds(race_id, bt) for bt in ODDS_TYPES if bt != "tan"}
