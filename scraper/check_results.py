"""
結果チェック・反映（軽量・高頻度実行用）

weekly_pipeline.py（重い：毎回モデル再学習）とは別に、土日のレース開催中に
頻繁に実行することを想定した軽量スクリプト。
  1. 予想済みだがまだ結果が出ていないレースを探す
  2. 結果が出ていれば取り込む
  3. 追跡中の買い目（tracked_bets）を的中判定する
  4. 該当レースのページに「結果」タブを追加して再生成する
  5. 収支ページも更新する

モデルの再学習は行わないため、数分〜十数分で完了する想定。
"""
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from db.database import (
    init_db, get_conn, races_needing_result_check, get_race_result_with_predictions,
    all_resolved_bets, unresolved_bets,
)
from scraper.netkeiba_scraper import fetch_race_result, save_to_db
from scraper.bulk_collect import _git_commit_and_push
from betting.resolve_bets import resolve_all
from static_site.generate_site import generate_performance_page, update_race_result_section


def check_and_update_results():
    init_db()
    with get_conn() as conn:
        target_ids = races_needing_result_check(conn)

    if not target_ids:
        print("結果確認が必要なレースはありません")
        return

    print(f"結果確認対象: {len(target_ids)}件")
    updated = 0

    for race_id in target_ids:
        try:
            result_df = fetch_race_result(race_id)
        except Exception as e:
            # レースがまだ終わっていない場合もここに来る（想定内なので静かにスキップ）
            print(f"  - {race_id}: まだ結果が確定していません（{e}）")
            continue

        if result_df is None or result_df.empty:
            continue

        save_to_db(result_df)  # 列名変換・finish_pos保存を含めて丸ごと任せる
        updated += 1
        print(f"  ✓ {race_id} の結果を取り込みました")

        # 予想と実際の結果を比較して、そのレースのページに「結果」タブを反映する
        try:
            with get_conn() as conn:
                comparison = get_race_result_with_predictions(conn, race_id)
            update_race_result_section(race_id, comparison)
        except Exception as e:
            print(f"  [警告] {race_id} の結果ページ更新に失敗: {e}")

    if updated == 0:
        print("新しく確定した結果はありませんでした")
        return

    print("=== 買い目の的中判定 ===")
    resolve_all()

    print("=== 収支ページを更新 ===")
    this_year = __import__("datetime").date.today().year
    with get_conn() as conn:
        summary_bets = all_resolved_bets(conn, year=this_year, strategy="value")
        display_bets = all_resolved_bets(conn, year=this_year, limit=100, strategy="value")
        pending = len([b for b in unresolved_bets(conn) if b.get("strategy", "value") == "value"])
        fav_summary_bets = all_resolved_bets(conn, year=this_year, strategy="favorite")
        fav_display_bets = all_resolved_bets(conn, year=this_year, limit=100, strategy="favorite")
        fav_pending = len([b for b in unresolved_bets(conn) if b.get("strategy") == "favorite"])
    generate_performance_page(
        summary_bets, display_bets, pending,
        favorite_summary_bets=fav_summary_bets, favorite_display_bets=fav_display_bets, favorite_pending_count=fav_pending,
    )

    _git_commit_and_push(f"result check: {updated}件のレース結果を反映")
    print(f"完了: {updated}件のレース結果を反映しました")


if __name__ == "__main__":
    check_and_update_results()
