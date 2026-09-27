# 開発ログ

## 2026-09-27

### Phase 1 着手: 公式API仕様の調査

- マネーフォワード クラウド会計API（OpenAPI v3）とAPIキー認証チュートリアルを確認
- APIキー認証は 2026-09-24 リリースの新機能
- 調査結果と設計案を [phase1-design.md](phase1-design.md) にまとめた
- リポジトリ初期化（`.gitignore` で `.env` と `data/` を除外）

**未決事項（実装前に確認）**

1. 認証用 `POST /auth/exchange` を唯一の例外として許可するか
2. 連携明細（`/api/v3/transactions`）も取得対象にするか
