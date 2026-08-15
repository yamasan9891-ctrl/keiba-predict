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
