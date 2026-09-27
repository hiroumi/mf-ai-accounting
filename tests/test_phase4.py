import json

import pytest

from mf_accounting import phase4


def test_sanitize_masks_identifiers_but_keeps_merchant_and_short_numbers():
    s = phase4.sanitize("カード 1234-5678-9012-3456 ABCマート 7 店 test@example.com 03-1234-5678 口座1234567")
    assert "ABCマート" in s and " 7 " in s
    for secret in ("1234-5678-9012-3456", "test@example.com", "03-1234-5678", "1234567"):
        assert secret not in s
    assert phase4.sanitize("****1234 AMAZON") == "<NUM> AMAZON"


def test_amount_band():
    assert phase4.amount_band(980) == "〜1千円" and phase4.amount_band(12000) == "1万〜5万円" and phase4.amount_band(2_000_000) == "100万円以上"


def test_schema_only_allows_catalog_codes():
    cat = phase4.Catalog.from_accounts([
        {"id": "X%3D", "name": "消耗品費", "available": True, "account_group": "EXPENSE", "category": "SGA"},
        {"id": "Y%3D", "name": "旧科目", "available": False, "account_group": "EXPENSE", "category": "SGA"},
    ])
    schema = phase4.output_schema(cat)
    enum = schema["properties"]["primary_account_code"]["anyOf"][0]["enum"]
    assert enum == ["A001"] and cat.code_to_id["A001"] == "X%3D"
    assert schema["additionalProperties"] is False and set(schema["required"]) == set(schema["properties"])


def pred(stage, n=5, agree=1.0, days=10, score=100.0):
    return {"stage": stage, "hist_n": n, "hist_agreement": agree, "days_since_last": days, "score": score}


def test_classify_target():
    assert phase4.classify_target(pred("raw_exact"))["high_confidence"]
    assert not phase4.classify_target(pred("raw_exact"))["llm_target"]
    assert phase4.classify_target(pred("none") | {"hist_n": 0})["categories"] == ["A_候補なし"]
    assert phase4.classify_target(pred("fuzzy", score=85))["categories"] == ["B_fuzzy候補のみ"]
    assert phase4.classify_target(pred("fuzzy", score=60, days=900))["categories"] == ["A_候補なし"]
    assert "C_科目が割れている" in phase4.classify_target(pred("raw_exact", agree=0.6))["categories"]
    assert "D_365日以上未使用" in phase4.classify_target(pred("raw_exact", days=400))["categories"]
    e = phase4.classify_target(pred("raw_exact", n=2))
    assert not e["high_confidence"] and not e["llm_target"]


def test_candidates_use_only_past_and_payload_excludes_answer():
    rows = [
        {"transaction_id": "p", "tx_date": "2024-01-01", "tx_content": "ABC SHOP 1234567", "primary_account_id": "X"},
        {"transaction_id": "same", "tx_date": "2024-02-01", "tx_content": "ABC SHOP", "primary_account_id": "Y"},
        {"transaction_id": "f", "tx_date": "2024-03-01", "tx_content": "ABC SHOP", "primary_account_id": "Z"},
    ]
    ordered = phase4.build_candidate_index(rows)
    c = phase4.candidates_before(ordered, "2024-02-01", "ABC SHOP", {"X": "A001"}, {"X": "消耗品費"})
    assert len(c) == 1 and c[0]["accounts"] == [{"code": "A001", "name": "消耗品費", "count": 1}]
    assert "1234567" not in c[0]["content"]
    payload = phase4.build_payload({"tx_content": "ABC SHOP", "tx_side": "EXPENSE", "tx_value": 1500, "tx_date": "2024-02-01",
                                    "actual": "Y", "transaction_id": "same"}, c, "銀行口座")
    text = phase4.user_text(payload)
    assert "same" not in text and '"Y"' not in text and "1500" not in text
    assert json.loads(text.split("\n", 1)[1])["transaction"]["amount_band"] == "1千〜5千円"


def test_run_llm_requires_approval():
    with pytest.raises(PermissionError):
        phase4.run_llm([], "", {}, None, "claude-opus-5", "medium", approved=False)
