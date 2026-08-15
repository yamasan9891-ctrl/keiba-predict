"""
予想レポート生成

model/predict.py で作った data/processed/prediction_<race_id>.csv を読み込み、
output/report_<race_id>.html を生成する。
"""
import argparse
import datetime as dt
from pathlib import Path

import pandas as pd
from jinja2 import Environment, FileSystemLoader

import sys
sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import DATA_PROCESSED_DIR, OUTPUT_DIR, RACE_ID_HELP


def generate(race_id: str) -> Path:
    pred_path = DATA_PROCESSED_DIR / f"prediction_{race_id}.csv"
    if not pred_path.exists():
        raise FileNotFoundError(
            f"{pred_path} がありません。先に model/predict.py --race-id {race_id} を実行してください。"
        )

    df = pd.read_csv(pred_path, encoding="utf-8-sig")

    name_col = "馬名" if "馬名" in df.columns else None
    rows = []
    for _, r in df.iterrows():
        rows.append({
            "horse_number": r.get("horse_number", "-"),
            "name": r.get(name_col, "-") if name_col else "-",
            "popularity": r.get("popularity", "-"),
            "win_odds": r.get("win_odds", "-"),
            "place_probability": float(r.get("place_probability", 0.0)),
        })

    env = Environment(loader=FileSystemLoader(str(Path(__file__).parent)))
    template = env.get_template("report_template.html")
    html = template.render(
        race_id=race_id,
        generated_at=dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        rows=rows,
    )

    out_path = OUTPUT_DIR / f"report_{race_id}.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"レポートを生成しました: {out_path}")
    return out_path


def main():
    parser = argparse.ArgumentParser(description="予想レポート生成")
    parser.add_argument("--race-id", required=True, help=RACE_ID_HELP)
    args = parser.parse_args()
    generate(args.race_id)


if __name__ == "__main__":
    main()


