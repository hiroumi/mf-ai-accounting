# mf-ai-accounting

マネーフォワード クラウド会計APIから自社の過去の仕訳と連携明細を取得し、将来のAIによる経費分類の参考データとして整備するプロジェクト。

## 現在のフェーズ

**Phase 1: 読み取り専用PoC**

- MF側への書き込み（仕訳の作成・変更・削除、明細の仕訳化）は行わない
- 会計API（`api-accounting.moneyforward.com`）は **GETのみ**。POST/PUT/PATCH/DELETE はコードレベルで送信前に拒否
- 唯一の例外は認証用 `POST https://api.biz.moneyforward.com/auth/exchange`（APIキー→JWT交換）
- APIキー・JWT・取得データはGitにコミットしない（`.gitignore` + pre-commit チェック）
- AIによる分類機能はまだ実装しない

## セットアップ（WSL / Linux）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt -e .
cp .env.example .env                                       # 実際の MF_API_KEY を設定
ln -sf ../../scripts/pre-commit.sh .git/hooks/pre-commit   # 秘密情報チェックを有効化
.venv/bin/python -m pytest                                 # 実APIには接続しないテスト
```

APIキーは [アプリポータル](https://developers.biz.moneyforward.com/docs/tutorials/api-keys/step-1-create-api-key) で発行します。
**クラウド会計で「閲覧」権限のみを持つユーザーで発行する** と、サーバー側でも書き込みが不可能になり安全です。

## 使い方（段階的に実行）

```bash
alias mf=".venv/bin/python -m mf_accounting"

mf offices                       # Step 1: 認証と accessible_offices（事業者番号を確認し .env に設定）
mf masters                       # Step 2: 事業者情報・会計期間・勘定科目・補助科目・税区分・部門・取引先・連携サービス
mf journals --sample 10          # Step 3: 仕訳を少量取得（既定: 開始済みの最新会計期間）
mf inspect journals              #         構造要約（値は表示しない）
mf transactions --sample 10      # Step 4: 連携明細を少量取得
mf inspect transactions
mf csv                           # Step 5: CSV変換
mf journals --all                # Step 6: 全会計期間を取得
mf transactions --all
mf csv
```

`--start YYYY-MM-DD --end YYYY-MM-DD` で少量取得の期間を指定できます。

## 出力

```
data/                                           # Git管理外
├── raw/{office_code}/{kind}/{日時}[-sample]/   # APIレスポンスJSON + manifest.json
└── processed/{office_code}/
    ├── journal_lines.csv                # 仕訳明細行（1 branch = 1行、借方/貸方を横持ち）
    ├── transactions.csv                 # 連携明細
    ├── transaction_journal_lines.csv    # 元明細 → 採用された仕訳
    └── accounts.csv など                # マスター
```

CSVはUTF-8（BOM付き）で、Excelでそのまま開けます。

## ドキュメント

- [Phase 1 設計（API仕様調査結果）](docs/phase1-design.md)
- [開発ログ](docs/dev-log.md)
- [開発上の注意事項](docs/dev-notes.md)（課税/免税ステータス等）
