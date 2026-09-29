"""MF クラウド会計の仕訳帳インポート CSV（生成と検証のみ。MF への import・API write は行わない）。

形式は MF の仕訳帳インポート用サンプル CSV と、MF 形式でエクスポートした既存仕訳 CSV に合わせる。
- 文字コード UTF-8（BOM なし）・LF。列はサンプルと同じ 27 列
- 1仕訳 = 同じ「取引No」を持つ複数行。1行 = 1明細行（branch）で、借方・貸方を同じ行に書く
- 金額・税額は整数。空欄側の金額は 0（サンプル・エクスポートと同じ）
"""

import csv
import io
from collections import Counter
from datetime import date
from pathlib import Path

COLUMNS = ["取引No", "取引日", "借方勘定科目", "借方補助科目", "借方部門", "借方取引先", "借方税区分", "借方インボイス", "借方金額(円)", "借方税額",
           "貸方勘定科目", "貸方補助科目", "貸方部門", "貸方取引先", "貸方税区分", "貸方インボイス", "貸方金額(円)", "貸方税額",
           "摘要", "仕訳メモ", "タグ", "MF仕訳タイプ", "決算整理仕訳", "作成日時", "作成者", "最終更新日時", "最終更新者"]
BLANK_COLUMNS = ("借方部門", "借方取引先", "借方インボイス", "貸方部門", "貸方取引先", "貸方インボイス", "仕訳メモ",
                 "タグ", "MF仕訳タイプ", "決算整理仕訳", "作成日時", "作成者", "最終更新日時", "最終更新者")


def read_csv(path: Path) -> tuple[list[dict], str]:
    """(行, 文字コード)。BOM の有無を区別する。"""
    raw = path.read_bytes()
    enc = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
    return list(csv.DictReader(io.StringIO(raw.decode(enc)))), enc


def to_rows(journal_lines: list[dict]) -> tuple[list[dict], list[dict]]:
    """v4 の final_journals（1行=1 branch）→（MF インポート行, 取引No と transaction_id の対応）。

    v4 の journal_no ごとに取引No を 1..N で振る。科目・補助科目・税区分・金額・摘要は v4 の値をそのまま使う。
    """
    rows, index = [], []
    order = sorted({int(l["journal_no"]) for l in journal_lines})
    no = {j: n for n, j in enumerate(order, start=1)}
    for l in sorted(journal_lines, key=lambda l: (int(l["journal_no"]), int(l["branch"]))):
        n = no[int(l["journal_no"])]
        row = dict.fromkeys(COLUMNS, "")
        row.update({
            "取引No": n, "取引日": l["transaction_date"].replace("-", "/"),
            "借方勘定科目": l["debit_account"], "借方補助科目": l["debit_sub_account"], "借方税区分": l["debit_tax"],
            "借方金額(円)": int(l["debit_amount"]), "借方税額": int(l["debit_tax_value"]),
            "貸方勘定科目": l["credit_account"], "貸方補助科目": l["credit_sub_account"], "貸方税区分": l["credit_tax"],
            "貸方金額(円)": int(l["credit_amount"]), "貸方税額": int(l["credit_tax_value"]),
            "摘要": l["remark"],
        })
        rows.append(row)
        if int(l["branch"]) == 1:
            index.append({"取引No": n, "journal_no": l["journal_no"], "transaction_id": l["transaction_id"], "transaction_date": l["transaction_date"]})
    return rows, index


def write(path: Path, rows: list[dict]) -> None:
    """サンプルと同じ UTF-8（BOM なし）・LF・必要時のみ引用符。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(rows)


def parse(rows: list[dict]) -> list[dict]:
    """MF 形式の行（インポート CSV・エクスポート CSV 共通）→ 内部形式（1行=1 branch）。"""
    out, branch = [], Counter()
    for r in rows:
        n = r["取引No"]
        branch[n] += 1
        out.append({"取引No": n, "branch": branch[n], "transaction_date": r["取引日"].replace("/", "-"),
                    "debit_account": r["借方勘定科目"], "debit_sub_account": r["借方補助科目"], "debit_tax": r["借方税区分"],
                    "debit_amount": int(r["借方金額(円)"] or 0), "debit_tax_value": int(r["借方税額"] or 0),
                    "credit_account": r["貸方勘定科目"], "credit_sub_account": r["貸方補助科目"], "credit_tax": r["貸方税区分"],
                    "credit_amount": int(r["貸方金額(円)"] or 0), "credit_tax_value": int(r["貸方税額"] or 0), "remark": r["摘要"],
                    "blank_columns_filled": [c for c in BLANK_COLUMNS if r.get(c)], "mf_type": r.get("MF仕訳タイプ", ""),
                    "created": r.get("作成日時", "")})
    return out


def journals(lines: list[dict]) -> dict:
    """取引No → その仕訳の行（branch 順）。"""
    out: dict = {}
    for l in lines:
        out.setdefault(l["取引No"], []).append(l)
    return out


def net_by(lines: list[dict], key) -> Counter:
    """key(line, side) ごとの 借方−貸方（科目が空の側は除く）。"""
    c = Counter()
    for l in lines:
        if l["debit_account"]:
            c[key(l, "debit")] += int(l["debit_amount"])
        if l["credit_account"]:
            c[key(l, "credit")] -= int(l["credit_amount"])
    return c


def legs(lines: list[dict]) -> Counter:
    """仕訳の (借/貸, 科目, 補助科目, 金額) の多重集合。行の分け方（1行に両側 / 片側ずつ）に依存しない比較用。"""
    c = Counter()
    for l in lines:
        if l["debit_account"] and int(l["debit_amount"]):
            c[("D", l["debit_account"], l["debit_sub_account"], int(l["debit_amount"]))] += 1
        if l["credit_account"] and int(l["credit_amount"]):
            c[("C", l["credit_account"], l["credit_sub_account"], int(l["credit_amount"]))] += 1
    return c


def duplicate_candidates(new: dict, existing: dict) -> list[dict]:
    """インポート予定の仕訳と既存 MF 仕訳の重複候補。自動で除外はしない。

    確度: high = 同日・同じ (借貸, 科目, 補助科目, 金額) の組がすべて一致 /
          medium = 同日・金額が一致し、その金額の科目が1つ以上一致 /
          low = ±3日以内・金額が一致し、その金額の科目が1つ以上一致
    """
    ex = []
    for no, ls in existing.items():
        lg = legs(ls)
        ex.append((no, ls, lg, {(k[1], k[3]) for k in lg}, {k[3] for k in lg}, date.fromisoformat(ls[0]["transaction_date"])))
    out = []
    for no, ls in new.items():
        lg = legs(ls)
        accts, amounts = {(k[1], k[3]) for k in lg}, {k[3] for k in lg}
        dt = date.fromisoformat(ls[0]["transaction_date"])
        for eno, els, elg, eaccts, eamounts, edt in ex:
            gap = abs((edt - dt).days)
            if gap > 3 or not (amounts & eamounts):
                continue
            if gap == 0 and lg == elg:
                level = "high"
            elif gap == 0 and accts & eaccts:
                level = "medium"
            elif accts & eaccts:
                level = "low"
            else:
                continue
            out.append({"level": level, "import_no": no, "existing_no": eno, "date": ls[0]["transaction_date"], "existing_date": str(edt),
                        "import_legs": lg, "existing_legs": elg, "import_remark": ls[0]["remark"], "existing_remark": els[0]["remark"],
                        "existing_created": els[0]["created"]})
    return out


def internal_duplicates(js: dict) -> list[dict]:
    """同じ CSV 内で、日付・(借貸, 科目, 補助科目, 金額) の組・摘要がすべて同じ仕訳のグループ（別明細の可能性があるので除外はしない）。"""
    groups: dict = {}
    for no, ls in js.items():
        groups.setdefault((ls[0]["transaction_date"], tuple(sorted(legs(ls).items())), ls[0]["remark"]), []).append(no)
    return [{"date": k[0], "legs": Counter(dict(k[1])), "remark": k[2], "nos": v} for k, v in groups.items() if len(v) > 1]
