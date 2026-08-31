"""
展開予想図の生成

各馬の脚質（逃げ・先行・差し・追込）から、レース序盤〜中盤の隊列イメージを
横並びのレーン図（SVG）として可視化する。テキストだけでは伝わりにくい
「どういう展開になりそうか」を視覚的に示すのが狙い。
"""

STYLE_ORDER = ["逃げ", "先行", "差し", "追込"]
STYLE_LABELS = {
    "逃げ": "逃げ",
    "先行": "先行",
    "差し": "差し",
    "追込": "追込",
}
STYLE_COLORS = {
    "逃げ": "#d64550",
    "先行": "#f0a202",
    "差し": "#4caf6e",
    "追込": "#5b8ac9",
}


def build_pace_diagram_svg(horses: list, predicted_pace: str = None) -> str:
    """
    horses: [{"horse_number": ..., "running_style": "逃げ"|"先行"|"差し"|"追込"|None}, ...]
    predicted_pace: "ハイペース" | "スローペース" | "平均" | None

    戻り値: SVG文字列（そのままHTMLに埋め込み可能）
    """
    by_style = {s: [] for s in STYLE_ORDER}
    unknown = []
    for h in horses:
        style = h.get("running_style")
        num = h.get("horse_number")
        if style in by_style:
            by_style[style].append(num)
        else:
            unknown.append(num)

    lane_height = 56
    chip_size = 30
    chip_gap = 8
    left_margin = 56
    top_margin = 54
    width = 720
    n_lanes = len(STYLE_ORDER)
    height = top_margin + n_lanes * lane_height + (30 if unknown else 0) + 20

    svg_parts = [
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
        f'style="width:100%; height:auto; font-family:sans-serif;">'
    ]

    # 想定ペースの見出し
    if predicted_pace:
        pace_color = {"ハイペース": "#d64550", "スローペース": "#4caf6e", "平均": "#8d9690"}.get(predicted_pace, "#8d9690")
        svg_parts.append(
            f'<text x="0" y="20" font-size="14" font-weight="800" fill="{pace_color}">'
            f'想定ペース: {predicted_pace}</text>'
        )

    # 先頭→後方の向きを示す矢印ライン
    svg_parts.append(
        f'<line x1="0" y1="{top_margin - 12}" x2="{width - 10}" y2="{top_margin - 12}" '
        f'stroke="#26332c" stroke-width="1"/>'
    )
    svg_parts.append(
        f'<text x="0" y="{top_margin - 18}" font-size="10" fill="#8d9690">先頭側</text>'
    )
    svg_parts.append(
        f'<text x="{width - 40}" y="{top_margin - 18}" font-size="10" fill="#8d9690">後方側</text>'
    )

    for i, style in enumerate(STYLE_ORDER):
        y = top_margin + i * lane_height
        color = STYLE_COLORS[style]

        # レーンの背景帯
        svg_parts.append(
            f'<rect x="0" y="{y}" width="{width}" height="{lane_height - 8}" '
            f'fill="{color}" fill-opacity="0.06" rx="6"/>'
        )
        # レーンラベル
        svg_parts.append(
            f'<text x="8" y="{y + (lane_height - 8) / 2 + 4}" font-size="12" font-weight="700" fill="{color}">'
            f'{STYLE_LABELS[style]}</text>'
        )

        # 馬番チップ
        nums = by_style[style]
        for j, num in enumerate(nums):
            cx = left_margin + j * (chip_size + chip_gap) + chip_size / 2
            cy = y + (lane_height - 8) / 2
            svg_parts.append(f'<circle cx="{cx}" cy="{cy}" r="{chip_size/2}" fill="{color}"/>')
            svg_parts.append(
                f'<text x="{cx}" y="{cy + 4}" font-size="13" font-weight="800" fill="#0b0f0d" '
                f'text-anchor="middle">{num}</text>'
            )

    if unknown:
        y = top_margin + n_lanes * lane_height
        svg_parts.append(
            f'<text x="8" y="{y + 12}" font-size="11" fill="#8d9690">脚質データ不明: '
            f'{"、".join(str(n) for n in unknown)}</text>'
        )

    svg_parts.append("</svg>")
    return "".join(svg_parts)
