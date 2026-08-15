# keiba-predict

JRA（中央競馬）向けの「収集 → 蓄積 → 予想 → 買い目提示」システムです。
**馬券の自動購入は行いません。** 予想・買い目を見て、実際の購入操作はご自身でIPATから行ってください。

## 全体アーキテクチャ（2026年8月版）

```
[週次自動実行: GitHub Actions]
  1. scraper/         … 先週分の確定結果をDBに追加収集（10年分を少しずつ蓄積）
  2. db/database.py    … SQLiteに全データを保存（data/keiba.db をgit管理し永続化）
  3. features/         … 上がり3F・脚質・馬場状態・ハンデ斤量などを特徴量化
  4. model/            … LightGBMで複勝圏内確率を再学習
  5. betting/          … 単勝/馬連/馬単/3連複/3連単/WIN5のEVを計算、理由文を生成
  6. static_site/       … 今週のレース一覧＋各レース予想ページ(HTML)を生成
  7. GitHub Pages       … dist/ を無料で公開（サーバー代ゼロ、外出先からも閲覧可）
```

**なぜこの構成か**: 無料ホスティング（Render等）は再起動でデータが消えることが多く、
「10年分を蓄積してどんどん賢くする」という要件に合いません。GitHubリポジトリ自体を
データベースの保管場所として使うことで、完全無料かつデータが失われない構成にしています。

## 各ファイルの役割（改修したいときの目印）

| やりたいこと | 触るファイル |
|---|---|
| 予想の理由文の言い回しを変えたい | `betting/reasoning.py` |
| 特徴量（新しい指標）を追加したい | `features/feature_engineering.py`, `db/database.py`（列追加） |
| サイトの見た目・デザインを変えたい | `static_site/templates/index.html`, `static_site/templates/race.html` |
| EVの計算式・馬券種を追加したい（ワイド等） | `betting/ev_engine.py` |
| 自動実行の曜日・時間を変えたい | `.github/workflows/weekly.yml` の cron |
| 学習に使う特徴量を選び直したい | `features/feature_engineering.py` の `FEATURE_COLS` |
| netkeibaのHTML構造が変わって取得できなくなった | `scraper/netkeiba_scraper.py` の `SELECTORS`, `scraper/odds_scraper.py` |

各モジュールは独立して単体テストしやすい形にしてあるので（例: `python3 -c "from betting.ev_engine import ..."`）、
一部だけ直して壊れていないか確認しながら少しずつ改修できます。

## セットアップ（初回・ローカル）

```bash
pip install -r requirements.txt
python db/database.py   # DB初期化
```

## 過去10年分データの貯め方（初回のみ・時間がかかります）

```bash
# 例: 1レースずつ収集（本番運用ではrace_idを自動列挙するループにする）
python scraper/netkeiba_scraper.py --race-id <過去レースID> --mode result --with-horse-history
```
※ 数万レース分を一度に取得すると非常に時間がかかり、netkeibaへの負荷にもなります。
  `weekly_pipeline.py` の `last_week_result_race_ids()` を実装し、
  「毎週ちょっとずつ過去に遡って収集する」ジョブとして少しずつ回すことを推奨します。

## 週次自動更新のセットアップ（GitHub Actions + GitHub Pages）

1. `weekly_pipeline.py` 内の `this_week_race_ids()` と `last_week_result_race_ids()` を実装する
   （netkeibaの開催カレンダーページからrace_idを組み立てるロジックを追加）
2. GitHubにリポジトリを作りpush
3. リポジトリの Settings → Pages → Source を「GitHub Actions」に設定
4. `.github/workflows/weekly.yml` が毎週木曜21時(JST)に自動実行され、
   データ収集→再学習→サイト生成→GitHub Pagesへの公開まで自動で行われます
5. 手動で今すぐ実行したい場合は GitHub の Actions タブから
   「Weekly Keiba Update」→「Run workflow」で即実行できます

## 動作確認（ローカルでダミーデータ表示のみ試す）

```bash
python weekly_pipeline.py --dry-run
# static_site/dist/index.html が骨格だけ生成されます
```

## 買い目・EVについて

- 単勝・馬連・馬単・3連複・3連単すべてでEV（期待値 = 的中確率×オッズ）を計算し、
  100%を超える買い目だけを一覧表示します。最もEVが高い1点は「最も期待値が高い買い目」として強調表示されます。
- WIN5は対象5レースそれぞれの1着候補上位を掛け合わせて組み合わせを提示します。
- **注意**: 的中確率はモデルによる推定値であり、実際の的中率と乖離する可能性があります。
  EVが100%を超えていても、長期的に見て必ず利益が出ることを保証するものではありません。
  過去データでのバックテスト（実際に的中していたかの検証）を行うことを強く推奨します。

## 免責・注意事項

- 本ソフトウェアは予想の参考情報を提供するのみで、的中や利益を保証しません。
- スクレイピング対象サイトの利用規約・robots.txtを必ず確認し、アクセス頻度に配慮してください。
- 馬券の購入は必ずJRA公式ルート（即PAT/A-PAT/JRAダイレクト/JRA公式アプリ等）から、ご自身の判断で行ってください。
- 投票は自己責任で。のめり込みには十分ご注意ください（困ったときは自己申告による利用停止制度もあります）。
