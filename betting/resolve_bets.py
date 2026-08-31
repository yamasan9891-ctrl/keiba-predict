"""
買い目の的中判定・収支確定

weekly_pipeline.py で記録した「追跡中の買い目」(tracked_bets) のうち、
対象レースの結果が既に確定しているものを見つけ、実際に的中したかどうかを
判定して収支を確定する。auto_backfillでレース結果が集まるたびに実行する想定。

的中判定のルール（馬券種ごと）:
  単勝: 1着馬の馬番と一致するか
  馬連: 1-2着馬の馬番（順不同）と一致するか
  馬単: 1-2着馬の馬番（着順通り）と一致するか
  3連複: 1-3着馬の馬番（順不同）と一致するか
  3連単: 1-3着馬の馬番（着順通り）と一致するか
"""
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from db.database import get_conn, init_db, unresolved_bets, resolve_bet
from scraper.bulk_collect import _git_commit_and_push


def _get_finish_order(conn, race_id: str, n: int) -> list:
    """指定レースの上位n着の馬番を着順順に返す。まだ結果が無ければ空リスト"""
    rows = conn.execute(
        "SELECT horse_number, finish_pos FROM entries "
        "WHERE race_id = ? AND finish_pos IS NOT NULL ORDER BY finish_pos LIMIT ?",
        (race_id, n),
    ).fetchall()
    if len(rows) < n:
        return []
    return [str(r["horse_number"]) for r in rows]


def _check_hit(bet_type: str, horse_numbers: list, finish_order_top3: list) -> bool:
    """馬券種ごとのルールで的中判定する"""
    picked = [str(h) for h in horse_numbers]
    if bet_type == "単勝":
        return len(finish_order_top3) >= 1 and picked[0] == finish_order_top3[0]
    if bet_type == "馬連":
        return set(picked) == set(finish_order_top3[:2])
    if bet_type == "馬単":
        return picked == finish_order_top3[:2]
    if bet_type == "3連複":
        return set(picked) == set(finish_order_top3[:3])
    if bet_type == "3連単":
        return picked == finish_order_top3[:3]
    return False


def resolve_all():
    init_db()
    with get_conn() as conn:
        pending = unresolved_bets(conn)

    if not pending:
        print("未確定の買い目はありません")
        return

    print(f"未確定の買い目: {len(pending)}件")
    resolved_count = 0

    for bet in pending:
        with get_conn() as conn:
            finish_top3 = _get_finish_order(conn, bet["race_id"], 3)
        if not finish_top3:
            continue  # まだ結果が確定していない

        horse_numbers = bet["horse_numbers"].split(",")
        won = _check_hit(bet["bet_type"], horse_numbers, finish_top3)
        payout = bet["stake"] * bet["odds"] if won else 0.0

        with get_conn() as conn:
            resolve_bet(conn, bet["id"], won=won, payout=payout)

        resolved_count += 1
        result_str = f"的中！払戻{payout:.0f}円" if won else "不的中"
        print(f"  {bet['race_label']} {bet['bet_type']} {bet['horses']}: {result_str}")

    if resolved_count > 0:
        _git_commit_and_push(f"resolve bets: {resolved_count}件の買い目を確定")
    print(f"完了: {resolved_count}件を確定しました")


if __name__ == "__main__":
    resolve_all()
