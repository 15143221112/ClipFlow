# 📥 douyin-distiller

> 抖音クリエイターのホーム → 一括収集 → MP4 ダウンロード → 音声文字起こし（ASR）→ テキスト補正 → 飛書 Bitable へ書き込み。
> 抖音動画を構造化テキストに変える、Web 操作の自動化パイプライン。

[English](README.en.md) · [日本語](README.ja.md) · [한국어](README.ko.md)

## 特長
- **全自動パイプライン**：クリエイターホームまたは単一動画リンクを入力すると、収集 → ダウンロード → 音声抽出 → 文字起こし → 用語補正 → 飛書 Bitable 書き込みまで自動実行。
- **ローカル優先**：動画・音声・文字起こしはすべてローカル（SQLite + ファイル）に保存。クッキー失効後も再処理可能。
- **3 系統の ASR**：ローカル Whisper／iFlytek／飛書を UI で切替（デフォルトはローカル）。
- **LLM 補正**：句読点付与と専門用語の同音誤字を LLM（阿里 qwen-max またはローカル vLLM）で自動補正。
- **条件クエリ＋選択エクスポート**：状態／作者／日付／キーワード等で絞り込み、**現在の検索結果のみ**を CSV で書き出し。
- **DB 永続化**：作品と設定を SQLite（videos / settings テーブル）に保存。設定はページで即時反映。
- **飛書 Bitable 統合**：フィールド自動作成、aweme_id で upsert（存在すれば更新）。

## クイックスタート
```bash
git clone https://github.com/<you>/douyin-distiller.git && cd douyin-distiller
cp settings.example.json settings.json
docker compose up -d --build        # または: pip install -r requirements.txt && uvicorn app:app --port 9009
# ブラウザで http://<host>:9009
```

## ライセンス
[MIT](LICENSE)。抖音プラットフォームの規約と現地の法令を遵守した合法的な利用に限ります。
