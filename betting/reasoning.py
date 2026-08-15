"""
ルールベース理由生成

各馬の特徴量（脚質・上がり3F・馬場状態との相性・斤量ハンデ・逃げ馬の頭数など）から
人間が読みやすい理由文を自動で組み立てる。Claude APIなどのLLMは使わず、
テンプレート＋条件分岐だけで完結するため無料・オフラインで動く。
"""


def generate_reason(horse: dict, race_context: dict) -> str:
    """
    horse: 1頭分の特徴量辞書。想定キー:
      name, place_probability, running_style, last_3f, avg_last_3f,
      weight_carried, weight_carried_avg_when_win, horse_weight_diff,
      recent3_avg_finish_pos, place_rate, popularity, win_odds,
      is_handicap, track_condition, wet_condition_place_rate
    race_context: レース全体の情報。想定キー:
      n_nige (逃げ馬の頭数), track_condition, is_handicap
    """
    parts = []
    prob = horse.get("place_probability")
    if prob is not None:
        parts.append(f"モデル予想では複勝圏内（3着以内）に入る確率は約{prob*100:.0f}%。")

    # 脚質と展開
    style = horse.get("running_style")
    n_nige = race_context.get("n_nige")
    if style == "逃げ":
        if n_nige is not None and n_nige >= 3:
            parts.append(f"逃げ脚質だが、このレースは逃げ馬が{n_nige}頭おり先行争いが激しくなりペースが速まる可能性があるため、スタミナ面がやや不安要素。")
        elif n_nige is not None and n_nige <= 1:
            parts.append("逃げ脚質で、他に逃げ馬が少なく単騎で楽なペースを刻める可能性が高く、粘り込みに期待。")
        else:
            parts.append("逃げ脚質で、主導権を握れれば粘り込みが期待できる。")
    elif style == "先行":
        parts.append("先行脚質で、前目の位置から流れに乗りやすいタイプ。")
    elif style == "差し":
        parts.append("差し脚質で、直線での伸びに注目。展開が向けば台頭も。")
    elif style == "追込":
        parts.append("追込脚質で、前が止まる展開（速いペースの逃げ争いなど）になれば一気に浮上する可能性。")

    # 上がり3F
    last3f = horse.get("last_3f")
    avg3f = horse.get("avg_last_3f")
    if last3f and avg3f:
        if last3f <= avg3f - 0.3:
            parts.append(f"前走の上がり3Fは{last3f:.1f}秒とメンバー平均より速く、瞬発力に優れる。")
        elif last3f >= avg3f + 0.3:
            parts.append(f"前走の上がり3Fは{last3f:.1f}秒とやや平均より遅め。")

    # 馬場状態との相性
    condition = race_context.get("track_condition")
    wet_place_rate = horse.get("wet_condition_place_rate")
    if condition in ("重", "不良") and wet_place_rate is not None:
        if wet_place_rate >= 0.5:
            parts.append(f"当日の馬場は「{condition}」だが、この馬は過去の重馬場・不良馬場で複勝率{wet_place_rate*100:.0f}%と好走傾向。")
        elif wet_place_rate <= 0.2:
            parts.append(f"当日の馬場は「{condition}」で、この馬は過去の悪天候レースでやや苦戦気味（複勝率{wet_place_rate*100:.0f}%）。")

    # ハンデ戦の斤量
    if race_context.get("is_handicap"):
        wc = horse.get("weight_carried")
        wc_avg_win = horse.get("weight_carried_avg_when_win")
        if wc and wc_avg_win:
            if wc <= wc_avg_win - 1:
                parts.append(f"ハンデ戦において、過去の勝ち時の平均斤量({wc_avg_win:.1f}kg)より軽い{wc}kgで出走しており、斤量面で有利。")
            elif wc >= wc_avg_win + 1.5:
                parts.append(f"ハンデ戦において、過去の勝ち時の平均斤量({wc_avg_win:.1f}kg)より重い{wc}kgを背負っており、斤量面ではやや不利。")

    # 近走成績
    recent = horse.get("recent3_avg_finish_pos")
    if recent is not None:
        if recent <= 3:
            parts.append(f"直近3走の平均着順は{recent:.1f}着と好調を維持。")
        elif recent >= 8:
            parts.append(f"直近3走の平均着順は{recent:.1f}着とやや不振。")

    # 馬体重増減
    wdiff = horse.get("horse_weight_diff")
    if wdiff is not None:
        if wdiff >= 10:
            parts.append(f"馬体重は前走比+{wdiff:.0f}kgと大幅増。仕上がりに注意。")
        elif wdiff <= -10:
            parts.append(f"馬体重は前走比{wdiff:.0f}kgと大幅減。")

    if not parts:
        return "十分なデータがなく、詳細な分析はできませんでした。"

    return "".join(parts)


def summarize_race(race_context: dict) -> str:
    """レース全体の傾向コメント（逃げ馬の頭数・馬場状態など）"""
    parts = []
    n_nige = race_context.get("n_nige")
    if n_nige is not None:
        if n_nige >= 3:
            parts.append(f"逃げ馬が{n_nige}頭おり、先行争いが激化しやすくハイペース濃厚。差し・追込馬にも展開の恩恵が期待できる。")
        elif n_nige == 0:
            parts.append("明確な逃げ馬が不在で、スローペースになりやすく先行馬が有利になりやすい。")
        else:
            parts.append(f"逃げ馬は{n_nige}頭で、比較的落ち着いたペースが予想される。")

    condition = race_context.get("track_condition")
    if condition:
        parts.append(f"馬場状態は「{condition}」。")

    if race_context.get("is_handicap"):
        parts.append("ハンデ戦のため、斤量差による有利不利を考慮した評価が重要。")

    return "".join(parts) if parts else ""
