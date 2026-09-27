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
        phase4.run_llm([], "", {}, None, "claude-opus-5", "medium", approved=False)  # API を呼ばずに停止


class _Resp:
    def __init__(self, text, stop="end_turn"):
        self.stop_reason = stop
        self.content = [type("B", (), {"type": "text", "text": text})()]


def _cat():
    return phase4.Catalog.from_accounts([{"id": "X%3D", "name": "消耗品費", "available": True}])


def test_parse_output_validates_and_maps_code():
    out = phase4.parse_output(_Resp('{"primary_account_code":"A001","confidence":0.8,"reason":"r","needs_review":false,"insufficient_information":false}'), _cat())
    assert out["primary_account_id"] == "X%3D"


@pytest.mark.parametrize("text,stop", [
    ('{"primary_account_code":"A999","confidence":0.8,"reason":"r","needs_review":false,"insufficient_information":false}', "end_turn"),
    ('{"primary_account_code":"A001","confidence":1.5,"reason":"r","needs_review":false,"insufficient_information":false}', "end_turn"),
    ("not json", "end_turn"),
    ("{}", "refusal"),
    ("{}", "max_tokens"),
])
def test_parse_output_stops_on_unexpected(text, stop):
    with pytest.raises(phase4.LLMRunError):
        phase4.parse_output(_Resp(text, stop), _cat())


def test_count_tokens_requires_approval():
    with pytest.raises(PermissionError):
        phase4.count_tokens([], "", {}, "claude-opus-5", "medium", approved=False)


def test_reason_categories():
    assert phase4.reason_categories("過去の類似明細で一貫して使用") == ["過去履歴を根拠", "類似取引を根拠"]
    assert phase4.reason_categories("情報が不足しており判断できない") == ["情報不足"]
    assert phase4.reason_categories("") == ["その他"]


def test_token_breakdown_requires_approval():
    with pytest.raises(PermissionError):
        phase4.token_breakdown([], "", {}, ["claude-opus-5"], approved=False)


def test_model_options_haiku_has_no_adaptive_thinking():
    assert phase4.model_options("claude-haiku-4-5") == {"thinking": False, "effort": None}
    assert phase4.model_options("claude-opus-5")["thinking"] is True
    req = phase4._request_for("claude-haiku-4-5", "s", {"type": "object"}, "u", None, False)
    assert "thinking" not in req and req["output_config"] == {"format": {"type": "json_schema", "schema": {"type": "object"}}}


def test_reason_category_schema_and_prompt():
    cat = _cat()
    s = phase4.output_schema(cat, with_reason_category=True)
    assert s["properties"]["reason_category"]["enum"] == phase4.REASON_CATEGORY_ENUM
    assert "reason_category" in s["required"]
    assert "reason_category" not in phase4.output_schema(cat)["properties"]
    assert "reason_category" in phase4.system_text(cat, True) and "reason_category" not in phase4.system_text(cat, False)


def test_non_high_conf_exact_gets_e_category():
    e = phase4.classify_target(pred("raw_exact", n=2))
    assert e["categories"] == ["E_exact過去1〜2回"] and not e["llm_target"] and not e["high_confidence"]


def test_run_llm_stops_when_cost_exceeds_limit(monkeypatch):
    class FakeClient:
        class messages:
            @staticmethod
            def create(**kw):
                r = _Resp('{"primary_account_code":"A001","confidence":0.8,"reason":"r","needs_review":false,"insufficient_information":false}')
                r.model = kw["model"]
                r.usage = type("U", (), {"input_tokens": 1_000_000, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})()
                return r
    monkeypatch.setattr(phase4, "make_client", lambda max_retries=0: FakeClient())
    done = []
    with pytest.raises(phase4.LLMRunError):
        phase4.run_llm([{"transaction_id": str(i), "user_text": "u"} for i in range(5)], "s", {}, _cat(), "claude-sonnet-5", "medium",
                       approved=True, on_result=done.append, max_cost=3.0)
    assert len(done) == 2  # $2/件 → 2件目で$4 > $3 で停止
