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

### 全期の仕訳件数確認（`counts` コマンド追加）

- 各期 GET 1回・1件のみ取得し `metadata.total_count` を集計（仕訳の内容は保存・表示しない）

| 会計期間 | 仕訳総件数 | 通常仕訳（概数） |
|---|---:|---:|
| FY2017 2017-08〜2018-07 | 0 | 0 |
| FY2018 2018-08〜2019-07 | 865 | 約864〜865 |
| FY2019 2019-08〜2020-07 | 868 | 約867〜868 |
| FY2020 2020-08〜2021-07 | 862 | 約861〜862 |
| FY2021 2021-08〜2022-07 | 1,262 | 約1,261 |
| FY2022 2022-08〜2023-07 | 1,040 | 約1,039 |
| FY2023 2023-08〜2024-07 | 1,047 | 約1,046 |
| FY2024 2024-08〜2025-07 | 1,160 | 約1,159 |
| FY2025 2025-08〜2026-07 | 1 | 0（開始仕訳のみ） |
| 合計 | 7,105 | 約7,097 |

- 通常仕訳の概数は「総件数 − 開始仕訳（各期0〜1件）」。APIに仕訳種別の絞り込みがないため厳密値は全件取得時に確定
- 教師データの年度選定はユーザー判断待ち

### Step 3（再）: FY2024 通常仕訳の少量取得

- `journals --sample 10 --exclude-opening`（GET 1回で15件取得し開始仕訳1件を除外 → 10件保存）。FY2024 総件数 1,160
- 取引日 2024-08-30〜2024-12-27（APIの返却順は取引日順ではない）
- `journal_type=journal_entry`, `entered_by=JOURNAL_TYPE_NORMAL`（全件）。branches数 1行:8件 / 2行:2件
- `transaction_id` あり 10/10、`voucher_file_ids` あり 0/10、`memo` 0/10、`remark` 10/12行、取引先 1/22
- 税区分は各明細行の借方・貸方ごとに `tax_id` / `tax_name` / `tax_long_name` / `tax_value` と `invoice_kind` で保持

### Step 4: FY2024 連携明細の少量取得と紐付け確認

- `transactions --sample 10`（取引日昇順で最初の10件: 2024-08-01〜08-03）。FY2024 総件数 1,443
- フィールド: `id, date, value, side, content, memo, journalizing_status, connected_account_id, connected_sub_account_id, voucher_file_ids`
- 10件とも `side=EXPENSE`, `journalizing_status=registered`
- 新コマンド `linkcheck`: 明細IDで `GET /journals?transaction_ids=...` を実行 → **10/10 の明細が1件ずつの仕訳に紐付いた**
  - 取引日一致 10/10、連携口座の補助科目 = 仕訳の口座側補助科目 10/10
  - 金額: 仕訳の口座側（貸方−借方）= 明細金額 10/10。単純な借方合計では一致しない（税抜経理・複数行仕訳のため）
- IDはURLエンコード済み文字列（`%2B` 等）で返る。`transaction_ids` 指定時は一度デコードして渡す（二重エンコード防止、テスト追加）

### Step 5: CSV変換（少量データで確認）

- 入力: FY2024 仕訳サンプル10件 + `linkcheck` で取得した紐付き仕訳10件（計20件）、明細10件、マスター
- `training_pairs.csv` を新設（旧 `transaction_journal_lines.csv` を置き換え）
  - 1行 = (明細, 仕訳の明細行)。`journal_id` + `branch_index` / `branch_count` で複数行仕訳を復元可能
  - `fiscal_year` / `term_tax_method` / `term_accounting_method`（取引日→会計期間→課税方式）を保持
  - `bank_side`（明細行のどちら側が口座か）、`bank_net_amount` / `amount_matches_tx`（口座側純額と明細金額の一致）
- 結果

| ファイル | 行数 | 列数 |
|---|---:|---:|
| journal_lines.csv | 27（仕訳20件） | 47 |
| transactions.csv | 10 | 16 |
| training_pairs.csv | 15（仕訳10件） | 57 |

- 紐付け率 10/10（100%）、1明細あたり仕訳1件、複数行仕訳3件（2行×2, 4行×1）、金額一致 10/10
- 復元テスト: `training_pairs.csv` を読み戻し、元JSONの branches と明細行単位で 10/10 仕訳一致
- 全行空の列: memo・tags・部門（未使用）、貸方の取引先。借方/貸方の片側が空の明細行あり（一方のみの行）
