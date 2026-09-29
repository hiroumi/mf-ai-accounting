# 決算処理 Runbook（MF 連携明細 → 最終仕訳 → MF インポート）

MF クラウド会計の連携明細（銀行・カード）から、過年度仕訳を教師データとして勘定科目を推定し、
人間のレビューを経て最終仕訳を作り、MF 仕訳帳インポート CSV を生成・検証するまでの手順。
FY2025（2025-08-01〜2026-07-31）で初めて実施した。年度ごとの作業記録は [dev-log.md](dev-log.md) を参照。

コマンドはすべて `python -m mf_accounting <command>`（`.venv` を有効化した状態）。以下 `mf` と略記する。
`{FY}` は対象年度（例: 2025）、`<office>` は事業者番号。

## 0. 重要ルール

- **MF への書き込み・インポートはこのツールでは行わない。** 会計 API は GET のみ（`GuardedSession` が書き込みを遮断）。
  インポートは人間が MF 画面で実施する
- **人間の判断を AI・コードが上書きしない。** レビュー結果・`final_overrides.csv` の判断は、コードが勝手に変更・削除しない。
  重複候補・警告は報告するだけで、自動で除外しない
- **年度固有の判断はコードに埋め込まず、`final_overrides.csv` に残す**（理由つき）
- **validation の error / warning が1件でも残っている間は MF にインポートしない**
- **フォーマットを推測しない。** MF 形式は MF から取得した実際のサンプル・エクスポートを基準にする
- 取得データ・生成物（`data/` 以下）と `.env` は Git にコミットしない（`.gitignore` 済み、pre-commit で秘密情報をチェック）

## 1. 全体フロー

```
MF Cloud からデータ取得（masters / journals / transactions）
  ↓
過年度仕訳を教師データとして利用（明細 × 仕訳の紐付け）
  ↓
対象年度の明細を分類（Amazon 除外・層分け）        … mf close-prepare
  ↓
自動判定（高confidenceルール + Sonnet）             … mf llm-run
  ↓
human review（グループ単位）                       … mf close-review / close-groups
  ↓
final_overrides.csv で最終判断（明細単位）
  ↓
final journals 生成 + validation                   … mf close-final
  ↓
MF インポート CSV 生成 + validation                … mf close-mf-import
  ↓
既存 MF 仕訳との duplicate check（全期間）          … mf close-mf-import --check-only
  ↓
1件テストインポート（MF 画面・人間）
  ↓
本番インポート（MF 画面・人間）
  ↓
インポート後の最終照合
```

## 2. 決算開始前に取得するデータ

| データ | 取得方法 | 用途 |
|---|---|---|
| マスター（勘定科目・補助科目・税区分・会計期間・連携口座） | `mf masters` | 科目・補助科目・税区分の名前と ID の解決、連携口座（カード・銀行）の識別 |
| 過年度の仕訳 | `mf journals --all` | 教師データ（明細 → 実際に使われた主科目）。`data/raw/<office>/journals/` |
| 過年度の連携明細 | `mf transactions --all` | 仕訳と明細の紐付け（`transaction_id`）。`data/raw/<office>/transactions/` |
| 対象年度の連携明細・マスター | `MF_DATA_DIR=data/fy{FY}_close` を指定して `mf masters` / `mf transactions --all`（`MF_JOURNALS_START_DATE` / `MF_JOURNALS_END_DATE` で対象年度に限定） | 分類の対象。`close-*` コマンドは `data/fy{FY}_close/raw/<office>/{masters,transactions}` の最新取得分を使う |
| MF 仕訳帳エクスポート（対象年度の既存仕訳, MF 形式 CSV） | MF 画面から手動でエクスポートし `data/` に置く | 既存仕訳との重複チェック、MF 形式（列・文字コード）の確認。**対象年度の全期間**をカバーすること（期間ごとに複数ファイル可） |
| MF 仕訳帳インポート用サンプル CSV | MF 画面から手動でダウンロードし `data/` に置く | インポート CSV のテンプレート（列名・列順・文字コード） |

- 取得前に `mf counts` で各期の件数を確認できる
- 教師データの年度範囲は `cli._phase4_context`（現在 FY2018〜FY2024 に固定）。**次年度は対象年度の前年度まで含むよう変更が必要**（§12）

## 3. 分類・自動判定・AI レビュー

### 3.1 対象抽出と層分け（`mf close-prepare --fy {FY}`）

- Amazon 連携口座の明細はユーザーが MF 画面で手動処理するため対象外（`AMAZON_CONNECTED_ACCOUNT`）
- カード明細で摘要に AMAZON / アマゾン を含むもの（AWS を除く）は「Amazon 重複確認候補」として分離（`AMAZON_DUP_*`）
- カード更新・再登録・追加カードは、勘定科目推定上1つの口座系統として扱う（`ACCOUNT_LINEAGES`）
- 各明細を過年度履歴と照合し層に分ける: 高confidence（exact・3回以上・一致率100%・365日以内）/ A（候補なし）/ B（fuzzy のみ）/ C（科目が割れている）/ D（365日以上未使用）/ E（exact・過去1〜2回）
- 出力: `data/processed/<office>/phase4/fy{FY}_close/`（`payloads.jsonl`・`items.json` 等）と `data/fy{FY}_close/counts.json`

### 3.2 自動判定

- **rule_candidate**: 高confidence かつ 口座系統が過去履歴にある明細
- **Sonnet**: A / B / C / C+D / D を Sonnet に送る。prompt / schema / account catalog は過年度検証と同じもの
  ```
  mf llm-run --name fy{FY}_close --model claude-sonnet-5 --effort medium --approve --count-only --max-cost <上限>
  mf llm-run --name fy{FY}_close --model claude-sonnet-5 --effort medium --approve --max-cost <上限>
  ```
  - 先に `--count-only` で見積もりを確認。結果は1件ずつ保存され、停止時は `--resume` で再開（fallback なし）
- **sonnet_auto_candidate**: confidence ≥ `SONNET_MIN_CONF`（0.90）かつ needs_review=false・insufficient=false。
  閾値は過年度で事前登録した値で、**対象年度の結果を見て変更しない**
- E 層・それ以外は human_review

### 3.3 レビュー CSV

```
mf close-review --fy {FY}    # review_ai.csv（1行=1明細）, review_amazon_dup.csv
mf close-groups --fy {FY}    # review_groups.csv（1行=1グループ）, review_group_members.csv（グループ ⇔ 明細）
```

- 出力先: `data/fy{FY}_close/review/`
- `review_groups.csv` は routing × 加盟店キー × 推定科目でまとめ、人間確認を先頭に並べる。主な列:
  `proposed_account`（推定科目）、`inference_source`（rule / sonnet）、`confidence_range`、`past_accounts`（過去に使われた科目）、`reason`、`routing`（rule_candidate / sonnet_auto_candidate / human_review）
- 最終レビュー結果は、`review_groups.csv` に次の列を追加した CSV として作る（FY2025 は `data/review_groups_assisted_final.csv`）:

| 列 | 値 | 意味 |
|---|---|---|
| `assistant_recommendation` | `approve` | 推定科目（`proposed_account`）を採用 |
| | `correct` | `assistant_correction_account` の科目を採用（MF マスターに存在する科目名であること） |
| | `exclude` | 帳簿の仕訳生成対象から除外 |
| | `existing_auto_candidate` | rule_candidate / sonnet_auto_candidate の既存の自動判定をそのまま使う（human_review のグループには使えない） |
| | `ask_hiro` | 未確定（Hiro の確認待ち）。**1件でも残っていれば validation error** |
| `assistant_correction_account` | 科目名 | `correct` のときの科目 |
| `assistant_reason` | 文 | 判断理由 |

- `human_decision` 列は `close-final` では使わない（上書きもしない）。最終判断は `assistant_*` 列と `final_overrides.csv` で行う
- 判断の優先順位: `final_overrides.csv`（明細単位）＞ `assistant_recommendation`（グループ単位）

## 4. final_overrides.csv（明細単位の最終判断）

置き場所: `data/fy{FY}_close/review/final_overrides.csv`（Git 管理外）。
列: `transaction_id, action, counter_sub_account, target_transaction_id, account, reason`。
科目・補助科目は **MF マスターの名前**で指定し、ID に解決できなければ処理を停止する（`close.load_overrides`）。
`reason` には判断者・根拠・日付を書く。

| action | 用途 | 使う列 |
|---|---|---|
| `exclude` | 帳簿対象外にする（例: 前年度購入分の返金を当年度で処理しない） | — |
| `set_account` | 主科目を人間の確認結果で置き換える（例: 創業者個人口座への出金 → 長期借入金、補助金入金 → 売上高） | `account` |
| `set_counter_sub_account` | 主科目側の補助科目を指定する（例: 口座間振替の相手口座） | `counter_sub_account` |
| `confirm_refund` | 自動判定のカード返金を人間が確認済みにし、購入時と逆の仕訳を作る（既定では返金は `refund_review` で止まる） | — |
| `merged_into` | 口座間振替の出金・入金の両方の明細がある場合に、片側を統合先の1仕訳にまとめる | `target_transaction_id` |

- 同じ `transaction_id` を2回書くとエラー。`merged_into` の統合先を `exclude` にするとエラー
- 年度固有の判断（個別取引の除外・科目変更・振替の相手口座など）は、**コードの定数ではなくこのファイルに残す**。
  コードの定数（§12）は複数年度に共通する方針だけにする

## 5. 個人カード（創業者個人カード）の仕訳

資金源（funding source）は連携口座ごとに `close.FUNDING_SOURCES` で明示する（`founder_personal_card` / `corporate_card` / `bank` / `other`）。
一致しない口座は `unconfirmed` となり仕訳を作らない（**カード種別から推測しない**）。

個人カードの購入は、MF のカード明細との対応を保つため、1仕訳の中で2段階にする:

```
Dr 経費科目               / Cr 未払金[カード補助科目]
Dr 未払金[同じカード補助科目] / Cr 長期借入金          （長期借入金に補助科目は付けない）
```

- 実質は `経費 / 長期借入金`（創業者からの借入）
- 返金（カード明細の入金）は逆方向: `Dr 未払金[カード] / Cr 経費` + `Dr 長期借入金 / Cr 未払金[カード]`
- カードの返金は既定で `refund_review`（自動確定しない）。過年度購入・固定資産購入の返金は特に、費用科目を単純に反転しない。人間が判断して `confirm_refund` または `exclude`
- 主科目が未払金・長期借入金になったカード明細は `structure_review`（振替が崩れるため）
- 法人カード・銀行には長期借入金への振替を適用しない
- validation: 未払金は**同じカード補助科目内で、仕訳ごとに net 0**。長期借入金の純増 = 個人カード利用 − 返金

## 6. 銀行口座間振替

自社口座間の振替は損益に含めない:

```
Dr 普通預金[入金口座] / Cr 普通預金[出金口座]
```

- 出金側・入金側の両方の明細がある場合は二重計上しない。片側を `merged_into` で統合する（validation が二重計上を検出する）
- 片側（例: デジタル明細のない銀行）の明細がない場合、相手口座は `needs_bankbook_review` として一覧化される（`fy{FY}_bankbook_review*.csv`）。
  **相手口座は推測で設定しない。** 通帳等で人間が確認し、`set_counter_sub_account` で確定する。創業者個人口座への出金なら `set_account 長期借入金`（借入金返済）
- 参考として、過去仕訳で同じ口座・加盟店キーがどの補助科目で記帳されたかを `estimated_counter_account` に表示する（自動設定はしない）

## 7. final journals と validation（`mf close-final`）

```
mf close-final --fy {FY} --assisted <最終レビューCSV> --overrides data/fy{FY}_close/review/final_overrides.csv --suffix _v1
```

出力（`data/fy{FY}_close/final/`）: `fy{FY}_final_transactions{suffix}.csv`（1行=1明細）/ `fy{FY}_final_journals{suffix}.csv`（1行=1明細行）/
`fy{FY}_validation_report{suffix}.csv` / `fy{FY}_bankbook_review{suffix}.csv` / `fy{FY}_final_summary{suffix}.json`。
判断を変えたら suffix を上げて再生成し、過去の版は残す。

MF にインポートする前に、次がすべて満たされていること（`close.validate_final` と `close-final` の警告）:

| 区分 | 項目 |
|---|---|
| error | 借方合計 = 貸方合計（全体・**仕訳ごと**） |
| error | 金額0の仕訳行がない |
| error | exclude・統合済み（merged）の明細が仕訳に含まれていない |
| error | 未確定科目がない（仕訳対象の全明細に仕訳がある。unresolved / refund_review / structure_review / 科目なしが残っていない） |
| error | 統合済み明細の統合先に仕訳がある |
| error | `ask_hiro` が 0件 |
| error | 個人カードの未払金が同じカード補助科目・同額で相殺（net 0） |
| error | 対象年度の期間外の取引がない |
| error | 同一取引の重複仕訳がない |
| warning | 口座間振替が出金・入金の両方で仕訳化されていない（二重計上） |
| warning | 前年度に計上済みの購入に対する返金を、確認なしで exclude していない |
| warning | 前年度購入の返金を当年度の費用で逆仕訳していない（していれば要確認） |
| info | `needs_bankbook_review` = 0 |

**error または warning が1件でも残っている状態では MF にインポートしない。**
併せて `summary.json` の資金源別の検算（個人カード利用・返金・長期借入金の増減・未払金の貸借）を確認する。
事前確認用に `mf close-finalize --fy {FY} --preview`（推定科目での仮仕訳と検算）もある。

## 8. MF インポート CSV（`mf close-mf-import`）

MF から取得した実際のインポート用サンプル CSV をテンプレートにし、**フォーマットを推測しない**。
FY2025 で確認した形式（サンプル・エクスポートとも）:

- UTF-8（BOM なし）・LF。列は27列（取引No, 取引日, 借方勘定科目, 借方補助科目, 借方部門, 借方取引先, 借方税区分, 借方インボイス, 借方金額(円), 借方税額, 貸方勘定科目, …, 摘要, 仕訳メモ, タグ, MF仕訳タイプ, 決算整理仕訳, 作成日時, 作成者, 最終更新日時, 最終更新者）
- 日付 `YYYY/MM/DD`、金額・税額は整数、空欄側の金額は 0
- **複合仕訳 = 同じ「取引No」の複数行。** 1行に借方・貸方の両方を書ける。個人カードの2段階も1つの取引No（2行）
- 部門・取引先・インボイス・仕訳メモ・タグ・MF仕訳タイプ・決算整理仕訳・作成日時・作成者・最終更新日時・最終更新者は空欄
- MF のエクスポートでは、免税年度（FY2025）の仕訳の税区分が空欄で出力される。インポート CSV は確定方針の「対象外」を使い、**本番前に1件テストインポートで受理を確認する**（§10）

**MF のサンプル・エクスポートの形式が変わっていないか、毎年確認する**（検証 F1 が列名・列順をサンプルと比較する）。

```
mf close-mf-import --fy {FY} --source-suffix _v4 \
  --overrides data/fy{FY}_close/review/final_overrides.csv \
  --sample data/journal_sample.csv \
  --export "data/仕訳データ_<開始>_<終了>.csv" --export-period <開始>:<終了> \
  --output fy{FY}_mf_import_v1.csv
```

- 出力: `fy{FY}_mf_import_v1.csv`（インポート CSV）、`_index.csv`（取引No ⇔ transaction_id）、`_validation.csv`、`_duplicate_candidates.csv`、`_summary.json`
- final journals の内容（日付・科目・補助科目・税区分・金額・摘要）は変更しない。統合・分割・簡略化しない
- 検証: 形式（F1〜F4）、仕訳数・借方/貸方合計・科目別/補助科目別 net の一致、欠落・0円・重複・exclude/merged の混入、科目・補助科目が MF マスターに存在し名前が一意、税区分、**書き出した CSV を読み直して final journals と全項目一致（R）**
- 検証 14〜18 は FY2025 の個別取引（返金・補助金・振替・返済）を確認する項目。次年度はその年の個別判断に合わせて見直す（§12）

## 9. 既存 MF 仕訳との duplicate check

本番インポート前に、**対象年度の全期間**の既存 MF 仕訳（MF 画面からエクスポート）と照合する。インポート CSV は変更しない:

```
mf close-mf-import --fy {FY} --check-only --report-suffix _dupcheck_full \
  --overrides data/fy{FY}_close/review/final_overrides.csv --sample data/journal_sample.csv \
  --export "data/仕訳データ_<期間1>.csv" --export-period <開始1>:<終了1> \
  --export "data/仕訳データ_<期間2>.csv" --export-period <開始2>:<終了2>
```

- 仕訳を (借/貸, 科目, 補助科目, 金額) の組で比較するので、行の分け方（1行に両側 / 片側ずつ）に依存しない
- 確度: **high** = 同日・全組一致 / **medium** = 同日・金額と科目が一致 / **low** = ±3日・金額と科目が一致。開始仕訳は除く
- **internal**: インポート CSV 内で日付・科目・金額・摘要がすべて同じ別明細の仕訳（実取引が2件か、明細の重複か）
- 候補は**自動で削除・変更しない。** 1件ずつ人間が判断し、除外するなら `final_overrides.csv` に `exclude` を書いて §7 から再生成する
- 検証 U で「照合未実施」が 0件（全期間カバー）であることを確認する
- エクスポート後に MF に仕訳が追加されることがあるため、**本番インポートの直前に全期間を再エクスポートして再実行**する

## 10. 1件テストインポート

- 通常の個人カード購入1件（2段階の2行を含む1仕訳。返金・override・重複候補でないもの）を、インポート CSV から同じ形式で抜き出す（FY2025 は `fy2025_mf_import_test1.csv`）
- MF 画面でインポートし、次を確認する:
  1. エラーなく取り込まれ、1仕訳（2行）になっている（2つに分かれていない）
  2. 科目・補助科目が一致し、新しい補助科目・科目が作られていない
  3. 税区分が受理され、税額が計算されていない（0）
  4. 摘要が文字化けしていない
  5. 未払金[カード] が +/− で相殺され、長期借入金が増えている
  6. 連携明細は未処理のまま（CSV インポートは連携明細と紐付かない）
- **テスト仕訳と本番の二重取り込みを防ぐ**: テスト仕訳を削除してから全件を取り込むか、全件 CSV からテスト分を除いて取り込む

## 11. 本番インポートとインポート後の最終照合

- 本番インポートは人間が MF 画面で行う。validation error / warning が 0、duplicate check の候補がすべて判断済みであることが前提
- CSV インポートした仕訳は連携明細と紐付かない。連携明細の扱い（未処理のまま残す等）は年度ごとに決める（FY2025 は未処理のままとし、このワークフローでは変更しない）
- インポート後の最終照合（手順。専用コマンドはない）:
  1. MF から対象年度の仕訳を再エクスポートする
  2. `close-mf-import --check-only` をそのエクスポートで再実行し、インポートした全仕訳が `high` で見つかること（件数 = インポート仕訳数）、想定外の候補がないことを確認する
  3. MF の試算表で、個人カードの未払金補助科目の残高・長期借入金・売上高などを `fy{FY}_final_summary*.json` の検算と照合する

## 12. 次年度に見直す設定・コード

| 場所 | 内容 | FY2025 の値 |
|---|---|---|
| `cli._phase4_context` | 教師データの年度範囲 | FY2018〜FY2024（固定）。前年度まで含むよう変更が必要 |
| `close.ACCOUNT_LINEAGES` | 1系統として扱うカード | 楽天カード（全サブ口座） |
| `close.FUNDING_SOURCES` | 連携口座ごとの資金源 | 楽天 Mastercard 系列・楽天 Visa 4235・Amex Delta = 個人カード、銀行3口座。**新しいカード（法人カード等）は口座単位で追加** |
| `close.FOUNDER_LOAN_ACCOUNT` | 個人カード立替の振替先 | 長期借入金 |
| `close.AMAZON_*` | Amazon の除外条件 | Amazon.co.jp 連携口座、AMAZON/アマゾン（AWS 除く） |
| `close.SONNET_*` | Sonnet 対象層・閾値 | A/B/C/C+D/D、confidence ≥ 0.90（過年度で事前登録し、対象年度を見て変えない） |
| `cli.cmd_close_final`・`cmd_close_mf_import` | 税区分 | 「対象外」・税額0（免税事業者）。**課税事業者の年度は税区分の扱いを別途設計する** |
| `cli.cmd_close_mf_import` 検証 14〜18 | 年度固有の個別取引の確認 | FY2025 の返金・補助金・振替・返済。その年の判断に合わせて見直す |

- 手作業で別途処理するもの（このワークフローの対象外）: みずほ銀行・給与・出張手当・現金取引、Amazon 連携明細と Amazon 重複確認候補
