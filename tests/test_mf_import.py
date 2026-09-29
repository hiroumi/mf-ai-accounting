from pathlib import Path

from mf_accounting import mf_import as mi


def v4line(no, br, dr, dsub, cr, csub, amt, date="2025-09-01", remark="SHOP", tid="t1"):
    return {"journal_no": str(no), "branch": str(br), "transaction_id": tid, "transaction_date": date,
            "debit_account": dr, "debit_sub_account": dsub, "debit_tax": "対象外", "debit_tax_value": "0", "debit_amount": str(amt),
            "credit_account": cr, "credit_sub_account": csub, "credit_tax": "対象外", "credit_tax_value": "0", "credit_amount": str(amt),
            "remark": remark}


def test_roundtrip_keeps_every_field_and_compound_journal(tmp_path: Path):
    v4 = [v4line(7, 1, "備品・消耗品費", "", "未払金", "楽天カード", 1000, remark="A,B \"x\""),
          v4line(7, 2, "未払金", "楽天カード", "長期借入金", "", 1000, remark="A,B \"x\""),
          v4line(9, 1, "普通預金", "城南信用金庫", "普通預金", "三井住友", 30000, tid="t2")]
    rows, index = mi.to_rows(v4)
    p = tmp_path / "import.csv"
    mi.write(p, rows)
    raw = p.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r" not in raw
    back, enc = mi.read_csv(p)
    assert enc == "utf-8" and list(back[0]) == mi.COLUMNS
    lines = mi.parse(back)
    assert [l["取引No"] for l in lines] == ["1", "1", "2"] and [i["transaction_id"] for i in index] == ["t1", "t2"]
    assert lines[0]["transaction_date"] == "2025-09-01" and back[0]["取引日"] == "2025/09/01"
    assert lines[1]["debit_sub_account"] == "楽天カード" and lines[1]["credit_account"] == "長期借入金" and lines[0]["remark"] == 'A,B "x"'
    assert all(not l["blank_columns_filled"] for l in lines)
    assert mi.net_by(lines, lambda l, s: (l[f"{s}_account"],))[("未払金",)] == 0


def test_legs_ignore_row_layout_and_duplicate_levels():
    one_row = [{"transaction_date": "2025-08-01", "remark": "x", "created": "", "debit_account": "消耗品費", "debit_sub_account": "", "debit_amount": 500,
                "credit_account": "長期借入金", "credit_sub_account": "", "credit_amount": 500}]
    split = [{**one_row[0], "credit_account": "", "credit_amount": 0},
             {**one_row[0], "debit_account": "", "debit_amount": 0}]
    assert mi.legs(one_row) == mi.legs(split)
    other_day = [{**one_row[0], "transaction_date": "2025-08-03"}]
    other_acct = [{**one_row[0], "debit_account": "会議費", "credit_account": "普通預金"}]
    got = {c["existing_no"]: c["level"] for c in mi.duplicate_candidates({"n": one_row}, {"a": split, "b": other_day, "c": other_acct})}
    assert got == {"a": "high", "b": "low"}


def test_internal_duplicates_groups_identical_journals():
    j = lambda d, amt, rem: [{"transaction_date": d, "remark": rem, "debit_account": "旅費交通費", "debit_sub_account": "", "debit_amount": amt,
                              "credit_account": "未払金", "credit_sub_account": "C", "credit_amount": amt}]
    got = mi.internal_duplicates({"1": j("2025-08-29", 20090, "T"), "2": j("2025-08-29", 20090, "T"), "3": j("2025-08-29", 20090, "U")})
    assert [g["nos"] for g in got] == [["1", "2"]]
