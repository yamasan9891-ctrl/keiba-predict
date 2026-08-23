"""
SQLiteデータベース

過去10年分を少しずつ蓄積していくためのストレージ層。
CSVと違い「同じレースを2回登録しない（重複防止）」「増分更新」がしやすい。

テーブル構成:
  races   ... レース単位の情報（開催日・コース・距離・馬場状態・ハンデ戦か否か等）
  entries ... 出走馬単位の情報（着順・斤量・上がり3F・脚質・オッズ等）※学習データにも予想対象にも使う
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import sys
sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import BASE_DIR

DB_PATH = BASE_DIR / "data" / "keiba.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS races (
    race_id TEXT PRIMARY KEY,
    race_date TEXT,
    course TEXT,           -- 競馬場（例: 東京, 阪神）
    distance INTEGER,       -- メートル
    surface TEXT,           -- 芝 / ダート
    track_condition TEXT,   -- 良 / 稍重 / 重 / 不良
    weather TEXT,           -- 晴 / 曇 / 雨 / 小雨 等
    is_handicap INTEGER DEFAULT 0,  -- ハンデ戦か（1/0）
    race_name TEXT,
    grade TEXT,              -- G1/G2/G3/OP等
    is_win5_race INTEGER DEFAULT 0,
    fetched_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS entries (
    race_id TEXT,
    horse_number INTEGER,
    post_position INTEGER,
    horse_id TEXT,
    horse_name TEXT,
    jockey_id TEXT,
    jockey_name TEXT,
    trainer_name TEXT,       -- 調教師（厩舎）
    weight_carried REAL,     -- 斤量
    horse_weight REAL,
    horse_weight_diff REAL,
    running_style TEXT,      -- 逃げ / 先行 / 差し / 追込（推定含む）
    last_3f REAL,            -- 上がり3F（秒）
    finish_time REAL,        -- 走破タイム（秒）※コース補正の基礎データ
    finish_pos INTEGER,      -- 確定済みレースのみ
    win_odds REAL,
    popularity INTEGER,
    is_placed INTEGER,       -- 3着以内か（学習ラベル）
    PRIMARY KEY (race_id, horse_number),
    FOREIGN KEY (race_id) REFERENCES races(race_id)
);

CREATE TABLE IF NOT EXISTS horses (
    horse_id TEXT PRIMARY KEY,
    horse_name TEXT,
    father TEXT,
    father_father TEXT,
    father_mother TEXT,
    mother TEXT,
    mother_father TEXT,
    mother_mother TEXT,
    fetched_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_entries_horse ON entries(horse_id);
CREATE INDEX IF NOT EXISTS idx_races_date ON races(race_date);
CREATE INDEX IF NOT EXISTS idx_horses_father ON horses(father);
CREATE INDEX IF NOT EXISTS idx_horses_motherfather ON horses(mother_father);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate_schema(conn)


def _migrate_schema(conn):
    """
    既存のDBファイルに、後から追加されたカラムがない場合に追加する
    （CREATE TABLE IF NOT EXISTSは既存テーブルにカラムを追加してくれないため）。
    """
    migrations = {
        "entries": [("finish_time", "REAL"), ("trainer_name", "TEXT")],
    }
    for table, columns in migrations.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col_name, col_type in columns:
            if col_name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
                print(f"[migration] {table}.{col_name} を追加しました")


def upsert_race(conn, race: dict):
    cols = list(race.keys())
    placeholders = ", ".join(["?"] * len(cols))
    update_cols = [c for c in cols if c != "race_id"]
    if update_cols:
        updates = ", ".join([f"{c}=excluded.{c}" for c in update_cols])
        sql = (
            f"INSERT INTO races ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(race_id) DO UPDATE SET {updates}"
        )
    else:
        sql = (
            f"INSERT INTO races ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(race_id) DO NOTHING"
        )
    conn.execute(sql, [race[c] for c in cols])


def upsert_entries(conn, entries: list[dict]):
    if not entries:
        return
    cols = list(entries[0].keys())
    placeholders = ", ".join(["?"] * len(cols))
    updates = ", ".join([f"{c}=excluded.{c}" for c in cols if c not in ("race_id", "horse_number")])
    sql = (
        f"INSERT INTO entries ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(race_id, horse_number) DO UPDATE SET {updates}"
    )
    for e in entries:
        conn.execute(sql, [e.get(c) for c in cols])


def race_exists(conn, race_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM races WHERE race_id = ?", (race_id,)).fetchone()
    return row is not None

def upsert_horse(conn, horse: dict):
    cols = list(horse.keys())
    placeholders = ", ".join(["?"] * len(cols))
    update_cols = [c for c in cols if c != "horse_id"]
    if update_cols:
        updates = ", ".join([f"{c}=excluded.{c}" for c in update_cols])
        sql = (
            f"INSERT INTO horses ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(horse_id) DO UPDATE SET {updates}"
        )
    else:
        sql = (
            f"INSERT INTO horses ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(horse_id) DO NOTHING"
        )
    conn.execute(sql, [horse[c] for c in cols])


def horse_exists(conn, horse_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM horses WHERE horse_id = ?", (horse_id,)).fetchone()
    return row is not None


def distinct_horse_ids_without_pedigree(conn) -> list:
    rows = conn.execute("""
        SELECT DISTINCT e.horse_id
        FROM entries e
        LEFT JOIN horses h ON e.horse_id = h.horse_id
        WHERE e.horse_id IS NOT NULL AND h.horse_id IS NULL
    """).fetchall()
    return [r["horse_id"] for r in rows]


if __name__ == "__main__":
    init_db()
    print(f"DB初期化しました: {DB_PATH}")
