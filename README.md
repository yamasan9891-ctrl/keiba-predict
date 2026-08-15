# keiba-predict

JRA（中央競馬）向けの「収集 → 予想 → レポート表示」システムです。
**馬券の自動購入は行いません。** 予想レポートを見て、最終的な購入操作はご自身でIPAT（即PAT/A-PAT/JRAダイレクト等）から行ってください。
（JRAの利用約定では、公式アプリ・公式ルート以外での自動送信を禁止・非保証としているため）

## 全体の流れ

```
[1] scraper/    … netkeibaなどから出馬表・過去成績・オッズを収集し data/raw/ に保存
[2] features/   … 生データを特徴量テーブルに変換し data/processed/ に保存
[3] model/      … LightGBMで「複勝圏内に入る確率」を学習・推論
[4] dashboard/  … 予想結果を見やすいHTMLレポートとして output/ に出力
```

## セットアップ

```bash
pip install -r requirements.txt
```

## 使い方

### 1. データ収集
```bash
python scraper/netkeiba_scraper.py --race-id 202506050811
```
※ `race_id` はnetkeibaのレースURLに含まれる12桁のIDです（例: `https://db.netkeiba.com/race/202506050811/`）。
※ **重要**: netkeibaのHTML構造は不定期に変わります。このスクリプトのCSSセレクタは執筆時点のものなので、
  動かない場合は該当ページのHTMLを見てセレクタ（`scraper/netkeiba_scraper.py` 内の `SELECTORS` 辞書）を
  調整してください。また `robots.txt` を確認し、アクセス間隔（デフォルト2秒）を空けて節度を持って利用してください。

### 2. 特徴量作成
```bash
python features/feature_engineering.py
```

### 3. モデル学習（過去データがある程度溜まったら）
```bash
python model/train.py
```

### 4. 当日の予想レポート作成
```bash
python model/predict.py --race-id 202506050811
python dashboard/generate_report.py --race-id 202506050811
```
`output/report_<race_id>.html` が生成されます。ブラウザで開いて確認し、
納得したらご自身でIPATにログインして購入してください。

## Webアプリとして外出先からも使う（無料枠デプロイ）

このリポジトリには外部公開用の軽量Webアプリ（`app.py`）が含まれています。
**重い処理（過去レース収集・モデル学習）はローカルで行い、学習済みモデルだけをサーバーに置く**構成です。
サーバー側は「出馬表取得→予想→レポート表示」の軽い処理のみを行うので、無料枠でも動きます。

### 手順

1. ローカルで学習を済ませる
   ```bash
   python scraper/netkeiba_scraper.py --race-id <過去レースID> --mode result --with-horse-history
   # ↑ を複数レース分繰り返してデータを増やす
   python features/feature_engineering.py
   python model/train.py
   ```
   → `model/artifacts/model.pkl` が生成されます。

2. GitHubにリポジトリを作ってpushする（`model/artifacts/model.pkl` も含めてコミット）

3. [Render.com](https://render.com) で「New Web Service」→ GitHubリポジトリを選択
   - 本リポジトリの `render.yaml` を自動検出してくれます（Blueprint機能）
   - 環境変数 `APP_PASSWORD` に好きな合言葉を設定（第三者に使われないための簡易ロック）
   - Freeプランを選択（月750時間分無料。しばらくアクセスがないとスリープし、次のアクセス時に少し起動が遅くなります）

4. デプロイ完了後に発行されるURL（`https://xxxxx.onrender.com`）にスマホのブラウザからアクセスし、
   合言葉でログインしてレースIDを入力すれば予想レポートが表示されます。

### モデルを更新したいとき

ローカルで再学習 → `model/artifacts/model.pkl` を差し替えてgit push するだけで、Renderが自動的に再デプロイします。

## 免責・注意事項

- 本ソフトウェアは予想の参考情報を提供するのみで、的中や利益を保証しません。
- スクレイピング対象サイトの利用規約・robots.txtを必ず確認し、アクセス頻度に配慮してください。
- 馬券の購入は必ずJRA公式ルート（即PAT/A-PAT/JRAダイレクト/JRA公式アプリ等）から、ご自身の判断で行ってください。
- 投票は自己責任で。のめり込みには十分ご注意ください（困ったときは自己申告による利用停止制度もあります）。
