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

### Step 1: 認証と accessible_offices（完了）

- 初回は `/auth/exchange` が 401。原因は `.env.example` の `mf_api_prd_REPLACE_ME` の `REPLACE_ME` 部分だけを置き換えたため、先頭 `mf_api_prd_` が重複していたこと
- 実際のキーは `mf_api_pro_` で始まり、ハイフンを含む（公式サンプルの `mf_api_prd_` + 英数字32文字とは異なる）
- 対策:
  - `.env.example` を `MF_API_KEY=REPLACE_WITH_YOUR_API_KEY`（右辺全体を置き換える形式）に変更
  - `config.py` で先頭重複・`mf_api_` 以外の形式・ダミー値を検出してエラーにする
  - `check_secrets.py` のAPIキー検出パターンをハイフン・`pro` 等を含む形式に拡張
- 修正後、JWT取得に成功（有効期限3600秒）。アクセス可能な事業者は1件（法人、会計期間9期分）
- 取得結果は `data/raw/_all/accessible_offices/`（Git管理外）

### Step 2: 事業者情報・会計期間・マスター（完了）

| マスター | 件数 | 備考 |
|---|---|---|
| office | 1 | 会計期間9期 |
| term_settings | 9 | FY2017〜FY2025（2017-08-01〜2026-07-31） |
| accounts | 133 | 使用中120 / 未使用13（過去仕訳の参照用に全件保持） |
| sub_accounts | 37 | |
| taxes | 151 | |
| departments | 0 | 部門未使用 |
| trade_partners | 10 | |
| connected_accounts | 6 | 口座・カード等 22 |

気づいた点:

- 最新の会計期間は FY2025（〜2026-07-31）。FY2026 はまだMF上に作成されていない
- 会計方式が期によって異なる（FY2023–2024 は税抜/按分、その他の期は未設定/FREE）。AI分類で税区分を使う際は期ごとの違いに注意
- 補助科目名（5件）・連携口座名（3件）に7桁以上の数字 → 口座番号等の可能性。取引先10件はインボイス登録番号あり

### Step 3: 仕訳の少量取得（実施・確認待ち）

- 条件: FY2025（2025-08-01〜2026-07-31）、最大10件 → **総件数1件**（開始仕訳 `JOURNAL_TYPE_OPENING`、明細行13）
- FY2025 には通常の仕訳がまだ登録されていない可能性がある。代表的な構造の確認には別の期のサンプルが必要
- JSON構造は仕様どおり（`branches[].debitor/creditor` に科目・補助科目・税・取引先・部門）。借方/貸方の片側が `null` の明細行がある
- 課税/免税ステータスの期ごとの違いを [dev-notes.md](dev-notes.md) に記録
