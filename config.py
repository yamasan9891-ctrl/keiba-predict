"""
プロジェクト共通設定
"""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

DATA_RAW_DIR = BASE_DIR / "data" / "raw"
DATA_PROCESSED_DIR = BASE_DIR / "data" / "processed"
OUTPUT_DIR = BASE_DIR / "output"
MODEL_DIR = BASE_DIR / "model" / "artifacts"

for d in (DATA_RAW_DIR, DATA_PROCESSED_DIR, OUTPUT_DIR, MODEL_DIR):
    d.mkdir(parents=True, exist_ok=True)

# netkeibaへの最低アクセス間隔（秒）。サーバー負荷・規約配慮のため必ず空ける。
SCRAPE_INTERVAL_SEC = 2.0

# リクエストヘッダー（過度に偽装せず、通常ブラウザ相当の情報のみ）
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# 学習ターゲット: 複勝圏内（3着以内）に入ったか否か
TARGET_COL = "is_placed"

RACE_ID_HELP = "netkeibaのレースID（例: 202506050811）。db.netkeiba.com/race/<race_id>/ のID部分。"
