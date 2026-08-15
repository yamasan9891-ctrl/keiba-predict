"""
Webアプリ（外部公開用）

やること: レースIDを受け取り、出馬表を取得→学習済みモデルで予想→レポート表示
やらないこと: モデルの学習、馬券の購入（すべて手動）

必須の環境変数:
  APP_PASSWORD ... サイトに入るための合言葉（未設定だと起動を拒否する）

ローカル起動:
  export APP_PASSWORD=好きな合言葉
  python app.py
  → http://127.0.0.1:5000 にアクセス
"""
import os
from functools import wraps
from pathlib import Path

from flask import Flask, request, render_template, redirect, url_for, session, flash

import sys
sys.path.append(str(Path(__file__).resolve().parent))
from config import MODEL_DIR, OUTPUT_DIR
from scraper.netkeiba_scraper import fetch_shutuba, save_raw
from model.predict import predict
from dashboard.generate_report import generate

APP_PASSWORD = os.environ.get("APP_PASSWORD")
SECRET_KEY = os.environ.get("SECRET_KEY", os.urandom(24).hex())

app = Flask(__name__)
app.secret_key = SECRET_KEY

if not APP_PASSWORD:
    raise RuntimeError(
        "環境変数 APP_PASSWORD が未設定です。外部公開する前に必ず合言葉を設定してください。"
        "（例: export APP_PASSWORD='xxxxx'）"
    )


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == APP_PASSWORD:
            session["logged_in"] = True
            return redirect(request.args.get("next") or url_for("index"))
        flash("合言葉が違います")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    model_ready = (MODEL_DIR / "model.pkl").exists()
    reports = sorted(OUTPUT_DIR.glob("report_*.html"), reverse=True)
    report_ids = [p.stem.replace("report_", "") for p in reports]
    return render_template("index.html", model_ready=model_ready, report_ids=report_ids)


@app.route("/run", methods=["POST"])
@login_required
def run():
    race_id = request.form.get("race_id", "").strip()
    if not race_id.isdigit():
        flash("レースIDは数字で入力してください（例: 202506050811）")
        return redirect(url_for("index"))

    if not (MODEL_DIR / "model.pkl").exists():
        flash("学習済みモデルがありません。ローカルで model/train.py を実行し、"
              "model/artifacts/model.pkl をサーバーに配置してください。")
        return redirect(url_for("index"))

    try:
        df = fetch_shutuba(race_id)
        save_raw(df, "shutuba", race_id)
        predict(race_id)
        generate(race_id)
    except Exception as e:
        flash(f"エラーが発生しました: {e}")
        return redirect(url_for("index"))

    return redirect(url_for("report", race_id=race_id))


@app.route("/report/<race_id>")
@login_required
def report(race_id):
    report_path = OUTPUT_DIR / f"report_{race_id}.html"
    if not report_path.exists():
        flash("そのレースのレポートはまだありません")
        return redirect(url_for("index"))
    return report_path.read_text(encoding="utf-8")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
