# 開発ログ

## 2026-09-27

### Phase 1 着手: 公式API仕様の調査

- マネーフォワード クラウド会計API（OpenAPI v3）とAPIキー認証チュートリアルを確認
- APIキー認証は 2026-09-24 リリースの新機能
- 調査結果と設計案を [phase1-design.md](phase1-design.md) にまとめた
- リポジトリ初期化（`.gitignore` で `.env` と `data/` を除外）

### 設計の確定事項

1. 認証用 `POST https://api.biz.moneyforward.com/auth/exchange` のみPOSTの例外として許可。会計APIはGETのみ、POST/PUT/PATCH/DELETEはコードレベルで禁止
2. 作業ディレクトリは `/mnt/c/Users/hirou/Documents/mf-ai-accounting`
3. 連携明細（`/api/v3/transactions`）も取得対象。目的は「元明細 → 実際に採用した仕訳」の参考データ化。`journalize` 等の書き込みは行わない
4. 実行は Step 1〜6 の段階方式。Step 3/4 は少量取得で一旦止めて結果を報告する
5. AIによる分類機能はまだ実装しない

### Phase 1 実装

- 書き込み防止ガード `guard.py`: 許可リスト方式。`requests.Session.send` をオーバーライドし、リダイレクトを含むすべての送信直前に検査
- クライアントは `get` / `get_paginated` のみ（書き込みメソッドを持たない）
- JWTのキャッシュ（期限5分前に更新）、401で1回だけ再取得
- レート制限（3 req/秒）に合わせて0.4秒間隔。429/5xxは指数バックオフで再試行
- 仕訳は会計期間ごと、明細は365日区間ごとにページネーション取得
- CSV: 仕訳明細行・連携明細・明細↔仕訳の結合・各マスター
- `inspect` コマンド: 値を表示せずに構造・件数・期間・個人情報パターン該当件数を要約
- `scripts/check_secrets.py` + pre-commit フック: `.env`・`data/`・APIキー形式・JWT形式・`.env` の実際の値の混入を検出
- テスト81件（実APIには接続しない）すべて成功

**コミット前の確認**: ダミーの `.env`・`data/` ファイル・JWT文字列を一時的にステージし、チェッカーがすべて検出して拒否することを確認してから削除した。

**次**: `.env` に実際のAPIキーを設定 → Step 1（`offices`）
