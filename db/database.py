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

CREATE TABLE IF NOT EXISTS predictions (
    race_id TEXT,
    horse_number TEXT,
    horse_name TEXT,
    predicted_probability REAL,
    predicted_rank INTEGER,     -- そのレース内での予想順位（1位が最も確率高い）
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (race_id, horse_number)
);

CREATE TABLE IF NOT EXISTS tracked_bets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id TEXT,
    race_label TEXT,          -- 「札幌11R テスト記念」のような表示用ラベル
    bet_type TEXT,            -- 単勝/馬連/馬単/3連複/3連単
    horses TEXT,              -- 表示用の買い目文字列（例: "5 → 3 → 1"）
    horse_numbers TEXT,       -- 判定用にカンマ区切りの馬番だけを保存（例: "5,3,1"）
    odds REAL,
    predicted_probability REAL,
    predicted_ev REAL,
    stake INTEGER DEFAULT 5000,
    resolved INTEGER DEFAULT 0,   -- 0=未確定 1=確定済み
    won INTEGER,                   -- 0=外れ 1=的中（resolved=1の時のみ有効）
    payout REAL,                   -- 実際の払戻金額
    profit REAL,                   -- payout - stake
    created_at TEXT DEFAULT (datetime('now')),
    resolved_at TEXT
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
        "races": [("race_number", "TEXT"), ("has_prediction_page", "INTEGER DEFAULT 0")],
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
    """
    entriesに登場するが、まだhorsesテーブルに血統情報が無い馬のID一覧。
    「horsesテーブルに行自体が無い」馬に加えて、「行はあるが血統抽出に
    失敗してfatherが空のまま」の馬（過去のバグ等で取得漏れになったもの）も対象にする。
    """
    rows = conn.execute("""
        SELECT DISTINCT e.horse_id
        FROM entries e
        LEFT JOIN horses h ON e.horse_id = h.horse_id
        WHERE e.horse_id IS NOT NULL
          AND (h.horse_id IS NULL OR h.father IS NULL OR h.mother_father IS NULL)
    """).fetchall()
    return [r["horse_id"] for r in rows]


def insert_tracked_bet(conn, bet: dict):
    """1件の買い目を記録する（重複防止は replace_tracked_bets_for_race 側で行う）"""
    cols = list(bet.keys())
    placeholders = ", ".join(["?"] * len(cols))
    conn.execute(
        f"INSERT INTO tracked_bets ({', '.join(cols)}) VALUES ({placeholders})",
        [bet[c] for c in cols],
    )


def replace_tracked_bets_for_race(conn, race_id: str, bets: list):
    """
    指定レースの「まだ結果が確定していない」買い目記録を一旦削除してから、
    新しい複数点の買い目（購入プラン全体）をまとめて記録し直す。
    週次パイプラインが同じレースを複数回処理しても記録が重複・積み上がらないようにする。
    既に結果が確定済み(resolved=1)の記録は消さない。
    """
    conn.execute("DELETE FROM tracked_bets WHERE race_id = ? AND resolved = 0", (race_id,))
    for bet in bets:
        insert_tracked_bet(conn, bet)


def unresolved_bets(conn) -> list:
    rows = conn.execute("SELECT * FROM tracked_bets WHERE resolved = 0").fetchall()
    return [dict(r) for r in rows]


def resolve_bet(conn, bet_id: int, won: bool, payout: float):
    profit = payout - (conn.execute("SELECT stake FROM tracked_bets WHERE id = ?", (bet_id,)).fetchone()["stake"])
    conn.execute(
        "UPDATE tracked_bets SET resolved=1, won=?, payout=?, profit=?, resolved_at=datetime('now') WHERE id=?",
        (1 if won else 0, payout, profit, bet_id),
    )


def all_resolved_bets(conn) -> list:
    rows = conn.execute("SELECT * FROM tracked_bets WHERE resolved = 1 ORDER BY resolved_at DESC").fetchall()
    return [dict(r) for r in rows]


def mark_prediction_page_generated(conn, race_id: str):
    """このレースの予想ページを生成したことを記録する（アーカイブ一覧に出すため）"""
    conn.execute("UPDATE races SET has_prediction_page = 1 WHERE race_id = ?", (race_id,))


def list_archived_races(conn, limit: int = 300) -> list:
    """予想ページを生成したことがある全レースを、新しい順（race_id降順）で返す"""
    rows = conn.execute(
        "SELECT race_id, race_date, course, race_number, race_name, surface, distance "
        "FROM races WHERE has_prediction_page = 1 ORDER BY race_id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def save_predictions(conn, race_id: str, predictions: list):
    """
    そのレースの各馬の予想確率・予想順位を保存する（後で実際の結果と比較するため）。
    predictions: [{"horse_number":..., "horse_name":..., "predicted_probability":..., "predicted_rank":...}, ...]
    """
    conn.execute("DELETE FROM predictions WHERE race_id = ?", (race_id,))
    for p in predictions:
        conn.execute(
            "INSERT INTO predictions (race_id, horse_number, horse_name, predicted_probability, predicted_rank) "
            "VALUES (?, ?, ?, ?, ?)",
            (race_id, str(p["horse_number"]), p.get("horse_name"), p.get("predicted_probability"), p.get("predicted_rank")),
        )


def get_predictions(conn, race_id: str) -> list:
    rows = conn.execute(
        "SELECT * FROM predictions WHERE race_id = ? ORDER BY predicted_rank", (race_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def races_needing_result_check(conn) -> list:
    """
    予想ページを生成済み(has_prediction_page=1)だが、まだ結果(finish_pos)が
    entriesに入っていないレースのIDを返す（結果チェック対象）。
    """
    rows = conn.execute("""
        SELECT DISTINCT r.race_id
        FROM races r
        WHERE r.has_prediction_page = 1
          AND EXISTS (SELECT 1 FROM entries e WHERE e.race_id = r.race_id AND e.finish_pos IS NULL)
    """).fetchall()
    return [row["race_id"] for row in rows]


def get_race_result_with_predictions(conn, race_id: str) -> list:
    """
    予想と実際の結果を突き合わせた一覧を返す（結果比較ページ用）。
    戻り値: [{horse_number, horse_name, predicted_rank, predicted_probability, finish_pos}, ...]
    """
    rows = conn.execute("""
        SELECT
            p.horse_number, p.horse_name, p.predicted_rank, p.predicted_probability,
            e.finish_pos
        FROM predictions p
        LEFT JOIN entries e ON e.race_id = p.race_id AND e.horse_number = p.horse_number
        WHERE p.race_id = ?
        ORDER BY p.predicted_rank
    """, (race_id,)).fetchall()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    init_db()
    print(f"DB初期化しました: {DB_PATH}")
