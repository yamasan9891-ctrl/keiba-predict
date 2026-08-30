"""
賭け金・期待値（EV）計算エンジン

モデルが出す「勝つ確率っぽいスコア」を Plackett-Luce モデルの強さパラメータとみなし、
そこから 1着/2着/3着の並び確率を計算する。単勝以外の馬券（馬連・馬単・3連複・3連単）は
実際のオッズがなければ期待値を出せないため、オッズ取得関数と組み合わせて使う。

EV（期待値） = 的中確率 × オッズ（払戻倍率）
EV > 1.0 (100%) の買い目だけを「期待値プラスの買い目」として抽出する。

注意:
  - Plackett-Luceは「強い馬ほど上位に来やすい」という単純化されたモデル。
    実際のレースには展開や不利など確率的に扱いにくい要素も多く、EV自体はあくまで
    モデルの誤差を含む推定値である点に留意してください（過去データでの検証＝
    バックテストを必ず行うことを推奨します）。
  - 馬連・馬単・3連複・3連単の実際のオッズは、出走頭数が多いと組み合わせ数が
    非常に多く（3連単で18頭なら4896通り）、全組み合わせのオッズをnetkeibaから
    取得するには専用のオッズページ（例: race.netkeiba.com/odds/index.html?type=...）
    を都度スクレイピングする必要があります。scraper/odds_scraper.py 側で対応してください。
"""
from itertools import permutations, combinations

import numpy as np


def normalize_strengths(scores: dict) -> dict:
    """
    モデルの確率スコア（0〜1）を Plackett-Luce の強さパラメータに変換する。
    確率をそのまま強さとして使い、合計を1に正規化する簡易実装。
    """
    total = sum(scores.values())
    if total <= 0:
        n = len(scores)
        return {k: 1 / n for k in scores}
    return {k: v / total for k, v in scores.items()}


def _pl_prob_order(order: list, strengths: dict) -> float:
    """Plackett-Luceモデルで、指定した着順(order)が実現する確率を計算"""
    remaining = dict(strengths)
    prob = 1.0
    for horse in order:
        s_sum = sum(remaining.values())
        if s_sum <= 0:
            return 0.0
        prob *= remaining[horse] / s_sum
        del remaining[horse]
    return prob


def win_probabilities(strengths: dict) -> dict:
    return dict(strengths)  # Plackett-Luceでは1着確率=強さの正規化値そのもの


def exacta_probabilities(strengths: dict) -> dict:
    """馬単（1着-2着の順序あり）の確率。キーは (1着馬, 2着馬)"""
    horses = list(strengths.keys())
    return {
        (a, b): _pl_prob_order([a, b], strengths)
        for a in horses for b in horses if a != b
    }


def quinella_probabilities(strengths: dict) -> dict:
    """馬連（1-2着の組み合わせ、順不同）の確率。キーは frozenset({a, b})"""
    ex = exacta_probabilities(strengths)
    result = {}
    for (a, b), p in ex.items():
        key = frozenset((a, b))
        result[key] = result.get(key, 0.0) + p
    return result


def trifecta_probabilities(strengths: dict) -> dict:
    """3連単（1-2-3着の順序あり）の確率。キーは (1着, 2着, 3着)"""
    horses = list(strengths.keys())
    result = {}
    for a, b, c in permutations(horses, 3):
        result[(a, b, c)] = _pl_prob_order([a, b, c], strengths)
    return result


def trio_probabilities(strengths: dict) -> dict:
    """3連複（1-2-3着の組み合わせ、順不同）の確率。キーは frozenset({a,b,c})"""
    tri = trifecta_probabilities(strengths)
    result = {}
    for (a, b, c), p in tri.items():
        key = frozenset((a, b, c))
        result[key] = result.get(key, 0.0) + p
    return result


def build_ev_table(strengths: dict, odds: dict, horse_names: dict = None, top_n: int = 20) -> dict:
    """
    各馬券種ごとにEV上位を計算する。
    odds は下記のキーを持つ辞書（取得できたものだけでよい）:
      odds["tan"]      : {horse: 単勝オッズ}
      odds["umaren"]   : {frozenset({a,b}): 馬連オッズ}
      odds["umatan"]   : {(a,b): 馬単オッズ}
      odds["sanrenpuku"]: {frozenset({a,b,c}): 3連複オッズ}
      odds["sanrentan"] : {(a,b,c): 3連単オッズ}
    戻り値: {bet_type: [ {horses, odds, probability, ev}, ... ] }  (EV降順)
    """
    horse_names = horse_names or {}

    def label(h):
        name = horse_names.get(h, str(h))
        return f"{h} {name}"  # 「馬番 馬名」の形式（実際の投票時に馬番で識別するため）

    def label_set(hs):
        return " - ".join(label(h) for h in hs)

    tables = {}

    if "tan" in odds:
        win_p = win_probabilities(strengths)
        rows = []
        for h, o in odds["tan"].items():
            p = win_p.get(h, 0.0)
            rows.append({"horses": label(h), "odds": o, "probability": p, "ev": p * o})
        tables["単勝"] = sorted(rows, key=lambda r: -r["ev"])[:top_n]

    if "umaren" in odds:
        qp = quinella_probabilities(strengths)
        rows = []
        for key, o in odds["umaren"].items():
            p = qp.get(key, 0.0)
            rows.append({"horses": label_set(key), "odds": o, "probability": p, "ev": p * o})
        tables["馬連"] = sorted(rows, key=lambda r: -r["ev"])[:top_n]

    if "umatan" in odds:
        ep = exacta_probabilities(strengths)
        rows = []
        for key, o in odds["umatan"].items():
            p = ep.get(key, 0.0)
            rows.append({"horses": f"{label(key[0])} → {label(key[1])}", "odds": o, "probability": p, "ev": p * o})
        tables["馬単"] = sorted(rows, key=lambda r: -r["ev"])[:top_n]

    if "sanrenpuku" in odds:
        tp = trio_probabilities(strengths)
        rows = []
        for key, o in odds["sanrenpuku"].items():
            p = tp.get(key, 0.0)
            rows.append({"horses": label_set(key), "odds": o, "probability": p, "ev": p * o})
        tables["3連複"] = sorted(rows, key=lambda r: -r["ev"])[:top_n]

    if "sanrentan" in odds:
        tfp = trifecta_probabilities(strengths)
        rows = []
        for key, o in odds["sanrentan"].items():
            p = tfp.get(key, 0.0)
            rows.append({"horses": f"{label(key[0])} → {label(key[1])} → {label(key[2])}", "odds": o, "probability": p, "ev": p * o})
        tables["3連単"] = sorted(rows, key=lambda r: -r["ev"])[:top_n]

    return tables


def best_bet(tables: dict, min_probability: float = 0.01) -> dict | None:
    """
    全馬券種を横断して、最もEVが高い1点を返す。
    ただし的中確率がmin_probability未満の買い目は対象外にする
    （的中確率が極端に低い組み合わせは、オッズが巨大なだけでEVが
    見かけ上大きくなりやすく、実際にはただのノイズであることが
    バックテストで確認されているため。「一番のおすすめ」として
    見せるには不適切）。
    """
    best = None
    for bet_type, rows in tables.items():
        for row in rows:
            if row["probability"] < min_probability:
                continue
            candidate = dict(row)
            candidate["bet_type"] = bet_type
            if best is None or candidate["ev"] > best["ev"]:
                best = candidate
            break  # 各券種、確率条件を満たす最初の行（EV最大のもの）だけ見れば十分
    return best


def positive_ev_rows(tables: dict, threshold: float = 1.0) -> dict:
    """EVがthreshold（デフォルト100%）を超える買い目だけを抽出"""
    return {
        bet_type: [r for r in rows if r["ev"] > threshold]
        for bet_type, rows in tables.items()
    }


def build_betting_plan(tables: dict, min_probability: float = 0.01, max_picks: int = 5) -> list:
    """
    全馬券種を横断して、実際に「複数点に分けて買う」ことを前提にした
    購入プラン（候補リスト）を作る。1点だけを勧めるのは現実の買い方と
    合わないため、信頼できる範囲でEVが高い順に複数点をまとめて返す。

    戻り値: [{bet_type, horses, odds, probability, ev}, ...]  EV降順、最大max_picks件
    """
    candidates = []
    for bet_type, rows in tables.items():
        for row in rows:
            if row["probability"] < min_probability:
                continue
            candidate = dict(row)
            candidate["bet_type"] = bet_type
            candidates.append(candidate)

    candidates.sort(key=lambda r: -r["ev"])
    return candidates[:max_picks]


def identify_value_horses(
    strengths: dict,
    tan_odds: dict,
    popularity: dict,
    horse_names: dict = None,
    popularity_threshold: int = 5,
    ev_threshold: float = 1.0,
    min_probability: float = 0.02,
    max_odds: float = 150.0,
) -> list:
    """
    「穴馬」抽出: 人気が低い（popularity_threshold番人気以下）のに、
    単勝の期待値がev_threshold（デフォルト100%）を超えている馬を探す。
    「みんなが思いつく人気馬」ではなく、データ上は妙味があるのに見過ごされがちな馬を目立たせる。

    ただし以下は除外する（見せかけの穴馬を弾くため）:
      - 予想勝率がmin_probability未満（極端に低い確率×高オッズはただのノイズになりやすい）
      - オッズがmax_odds超（あまりに高いオッズは、まだ票が集まっておらず
        市場評価として成立していないだけの可能性が高い）

    戻り値: [{horse, name, popularity, odds, probability, ev}, ...]  EV降順
    """
    horse_names = horse_names or {}
    win_p = win_probabilities(strengths)
    results = []
    for h, pop in popularity.items():
        if pop is None or pop < popularity_threshold:
            continue
        odds = tan_odds.get(h)
        if odds is None or odds > max_odds:
            continue
        p = win_p.get(h, 0.0)
        if p < min_probability:
            continue
        ev = p * odds
        if ev > ev_threshold:
            results.append({
                "horse": h,
                "name": horse_names.get(h, str(h)),
                "popularity": pop,
                "odds": odds,
                "probability": p,
                "ev": ev,
            })
    results.sort(key=lambda r: -r["ev"])
    return results
