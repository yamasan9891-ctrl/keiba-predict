"""
WIN5 予想

WIN5は指定された5レースすべての1着馬を当てる馬券。
各レースの「1着になる確率が高い馬（複数可）」を軸として抽出し、
5レース分の組み合わせを確率の高い順に提示する。

全パターンを網羅すると点数が膨大になるため（各レースで上位N頭を選んでも N^5通り）、
実用的な点数に絞るオプションを用意している。
"""
from itertools import product

import numpy as np


def select_win5_box(strengths: dict, odds: dict = None, popularity: dict = None) -> list:
    """
    1レース分の「ボックス買い」対象馬を選ぶ（netkeibaのAI予想と同じ考え方）。

    1着候補の確率（＝一番手の自信度）に応じて頭数を変える：
      80%以上（圧倒的な本命）        → 1頭のみ
      50%以上80%未満                → 2頭
      25%以上50%未満                → 3頭
      25%未満（大混戦・軸不在）      → 4頭
    さらに、上記で選ばれなかった馬の中に「人気は低いが期待値が高い」穴馬
    （popularity・oddsが分かる場合のみ判定）が居れば、追加で1頭まで加える。

    戻り値: 馬番のリスト（確率が高い順、穴馬を追加した場合は末尾に付く）
    """
    ranked = sorted(strengths.items(), key=lambda x: -x[1])
    if not ranked:
        return []

    top_prob = ranked[0][1]
    if top_prob >= 0.80:
        max_horses = 1
    elif top_prob >= 0.50:
        max_horses = 2
    elif top_prob >= 0.25:
        max_horses = 3
    else:
        max_horses = 4

    box = [h for h, _ in ranked[:max_horses]]

    # 穴馬の追加判定: box外の馬の中で、人気が低い(5番人気以下)のに
    # 期待値(確率×オッズ)が1.0(=100%)を超える馬がいれば1頭だけ追加する
    if odds and popularity:
        best_dark = None
        for horse, prob in ranked[max_horses:]:
            pop = popularity.get(horse)
            o = odds.get(horse)
            if pop is None or o is None or pop < 5:
                continue
            ev = prob * o
            if ev > 1.0 and (best_dark is None or ev > best_dark[1]):
                best_dark = (horse, ev)
        if best_dark:
            box.append(best_dark[0])

    return box


def build_win5_box_plan(race_boxes: list[list], unit_price: int = 100) -> dict:
    """
    5レース分のボックス（各レースの馬番リスト）から、組み合わせ点数と合計金額を計算する。
    race_boxes: [[race1の馬番,...], [race2の馬番,...], ..., [race5の馬番,...]]

    戻り値: {"combinations": 点数, "total_cost": 合計金額}
    """
    if len(race_boxes) != 5:
        raise ValueError("WIN5は5レース分のボックスが必要です")

    combinations = 1
    for box in race_boxes:
        combinations *= max(1, len(box))

    return {
        "combinations": combinations,
        "total_cost": combinations * unit_price,
    }


def race_win_candidates(strengths: dict, top_n: int = 3) -> list:
    """1レース分の強さ辞書から、1着候補の上位n頭を [(horse, prob), ...] で返す"""
    ranked = sorted(strengths.items(), key=lambda x: -x[1])[:top_n]
    return ranked


def build_win5_combinations(race_candidates: list[list], odds_total: float = None, max_combos: int = 50) -> list:
    """
    race_candidates: 5レース分の [(horse, prob), ...] のリスト（長さ5）
    各レースから1頭ずつ選ぶ全組み合わせの的中確率を計算し、確率の高い順に返す。

    odds_total を渡すと（当日発表されるWIN5オッズ、または過去の平均配当などから概算した値）、
    EV = 的中確率 × odds_total としてEVも計算する。WIN5は的中者数で山分けの配当なので
    厳密なオッズは購入時点では確定しないことに注意。
    """
    if len(race_candidates) != 5:
        raise ValueError("WIN5は5レース分の候補リストが必要です")

    combos = []
    for picks in product(*race_candidates):
        # picks = ((horse1, p1), (horse2, p2), ..., (horse5, p5))
        horses = [p[0] for p in picks]
        prob = 1.0
        for _, p in picks:
            prob *= p
        row = {"horses": horses, "probability": prob}
        if odds_total:
            row["ev"] = prob * odds_total
        combos.append(row)

    combos.sort(key=lambda r: -r["probability"])
    return combos[:max_combos]


def format_win5_report(race_labels: list[str], combos: list, top_n: int = 10) -> str:
    """買い目一覧をテキストで整形（レポート表示用）"""
    lines = [f"WIN5対象レース: {' / '.join(race_labels)}", ""]
    for i, c in enumerate(combos[:top_n], 1):
        horses_str = " - ".join(c["horses"])
        line = f"{i}. {horses_str}  的中確率 {c['probability']*100:.3f}%"
        if "ev" in c:
            line += f"  推定EV {c['ev']*100:.0f}%"
        lines.append(line)
    return "\n".join(lines)
