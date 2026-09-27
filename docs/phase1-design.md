# Phase 1 設計: 読み取り専用PoC

調査日: 2026-09-27

## 参照した公式情報

- [マネーフォワード クラウド会計API（OpenAPI v3）](https://developers.api-accounting.moneyforward.com/) / [openapi.yaml](https://developers.api-accounting.moneyforward.com/v3/openapi.yaml)
- [Rate Limiter](https://developers.api-accounting.moneyforward.com/rate_limiter)
- [APIキーによる認証の仕組み](https://developers.biz.moneyforward.com/docs/tutorials/api-keys/)
- [STEP 2. APIキーをJWTに交換する](https://developers.biz.moneyforward.com/docs/tutorials/api-keys/step-2-exchange-for-jwt)
- [APIキー認証リリースのお知らせ（2026-09-24）](https://biz.moneyforward.com/support/account/news/new-feature/202609242.html)

## 認証（APIキー → JWT）

```
① POST https://api.biz.moneyforward.com/auth/exchange
   Authorization: Bearer mf_api_prd_xxxxx     ← APIキー
   → {"access_token": "<JWT>", "token_type": "Bearer", "expires_in": 3600}

② GET https://api-accounting.moneyforward.com/api/v3/...?office_code=XXXX-XXXX
   Authorization: Bearer <JWT>
```

- JWTは1時間有効。メモリにキャッシュし、期限の5分前に再取得する
- APIキー認証では `office_code`（事業者番号, 形式 `XXXX-XXXX`）が必須
- 認可はAPIキーを発行したユーザーのクラウド会計権限（編集／閲覧／なし）がそのまま使われる。不足時は 403

### 読み取り専用の担保

- **サーバー側**: 「閲覧」権限のみのユーザーでAPIキーを発行する（推奨）
- **コード側**: HTTPクライアントに許可リスト方式のガードを入れる
  - `api-accounting.moneyforward.com` へは GET 以外を送信前に例外で拒否
  - POST は `api.biz.moneyforward.com/auth/exchange`（認証用）のみ許可
  - pytest で PUT/PATCH/DELETE と許可外 POST が拒否されることを検証

## 使用するエンドポイント（すべて GET）

| 用途 | エンドポイント | 必要な権限（APIキー） |
|---|---|---|
| 事業者一覧 | `/api/v3/accessible_offices` | なし |
| 事業者情報（接続テスト） | `/api/v3/offices` | 設定 > 事業者（閲覧） |
| 会計期間 | `/api/v3/term_settings` | 設定 > 事業者（閲覧） |
| 勘定科目 | `/api/v3/accounts` | 設定 > 勘定科目（閲覧） |
| 補助科目 | `/api/v3/sub_accounts` | 設定 > 勘定科目（閲覧） |
| 税区分 | `/api/v3/taxes` | 決算・申告（閲覧） |
| 部門 | `/api/v3/departments` | なし |
| 取引先 | `/api/v3/trade_partners` | 設定 > 取引先（閲覧） |
| 仕訳一覧 | `/api/v3/journals` | 会計帳簿（閲覧） |
| （任意）連携明細 | `/api/v3/transactions` | 連携サービスから入力（閲覧） |

使用しない: `POST/PUT/DELETE /journals`, `/vouchers`, `POST /transactions`, `/transactions/journalize`, `POST /trade_partners`

### 仕訳一覧の仕様

- ページネーション: `page`, `per_page`（既定10, 最大10000）。レスポンスの `metadata.total_pages` まで取得
- `start_date` か `end_date` のどちらかが必須。指定日を含む会計期間の仕訳のみ返るため、`term_settings` の期ごとにループ
- レート制限: 1トークンあたり 3 req/秒（トークン交換は 100 req/分）→ 約0.4秒間隔、429/5xx は指数バックオフでリトライ

## 環境変数

```dotenv
MF_API_KEY=mf_api_prd_xxxxxxxxxxxxxxxx   # 必須
MF_OFFICE_CODE=XXXX-XXXX                  # 必須
MF_JOURNALS_START_DATE=                   # 任意（空なら全会計期間。transactionsにも適用）
MF_JOURNALS_END_DATE=                     # 任意
MF_JOURNALS_PER_PAGE=1000                 # 任意（1〜10000）
MF_TRANSACTIONS_PER_PAGE=500              # 任意（10〜500）
MF_DATA_DIR=data                          # 任意
```

## ディレクトリ構成（実装）

当初案の `scripts/*.py` 個別スクリプトは、Step 1〜6 を順に実行しやすいよう単一CLI（`python -m mf_accounting <command>`）に統合した。pandas は不要になったため依存から外した。

```
mf-ai-accounting/
├── .env.example / .gitignore / README.md / pyproject.toml
├── requirements.txt          # requests, python-dotenv
├── requirements-dev.txt      # + pytest
├── src/mf_accounting/
│   ├── guard.py              # 書き込み防止ガード（許可リスト、Session.send で送信前に検査）
│   ├── config.py             # .env読込・検証（ダミー値検出）
│   ├── auth.py               # APIキー→JWT交換・キャッシュ（期限5分前に更新）
│   ├── client.py             # GET専用クライアント（レート制限、リトライ、ページング）
│   ├── endpoints.py          # 各GETエンドポイント、会計期間・366日分割
│   ├── storage.py            # JSON保存（manifest付き）
│   ├── transform.py          # JSON→CSV変換、明細↔仕訳の結合
│   ├── inspect_json.py       # 値を出さない構造要約・個人情報パターン検出
│   └── cli.py                # offices / masters / journals / transactions / inspect / csv
├── scripts/
│   ├── check_secrets.py      # コミット前の秘密情報チェック
│   └── pre-commit.sh         # Git pre-commit フック
├── tests/                    # モックで検証（実APIには接続しない）
└── data/                     # .gitignore対象
    ├── raw/{office_code}/{kind}/{日時}[-sample]/
    └── processed/{office_code}/
```

## 連携明細（transactions）の仕様メモ

- `start_date` と `end_date` の差は366日以内 → 365日ごとの区間に分割して取得
- `per_page` は 10〜500
- 仕訳側の `transaction_id` で元明細と結合し `transaction_journal_lines.csv` を作成（IDの一致は Step 4 で実データ確認）
- `POST /transactions/journalize`（仕訳化）は使用しない。ガードでも拒否される

## CSV形式

仕訳の明細行（branch）1つを1行にする。

- 仕訳単位: `journal_id, branch_index, number, transaction_date, term_period, journal_type, entered_by, is_realized, memo, tags, remark, transaction_id, voucher_file_count, create_time, update_time`
- 借方: `debit_account_id/name, debit_sub_account_id/name, debit_tax_id/name/long_name, debit_value, debit_tax_value, debit_department_id/name, debit_trade_partner_code/name, debit_invoice_kind`
- 貸方: 同項目を `credit_` 付きで
- 文字コード: UTF-8 BOM付き（Excel互換）
