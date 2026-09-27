import pytest

from mf_accounting.config import ConfigError, load_settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("MF_API_KEY", "MF_OFFICE_CODE", "MF_DATA_DIR", "MF_JOURNALS_START_DATE", "MF_JOURNALS_END_DATE"):
        monkeypatch.delenv(k, raising=False)


def write_env(tmp_path, body):
    p = tmp_path / ".env"
    p.write_text(body, encoding="utf-8")
    return p


def test_valid_key_with_hyphen_and_underscore(tmp_path):
    s = load_settings(write_env(tmp_path, "MF_API_KEY=mf_api_pro_abc-DEF_123\nMF_OFFICE_CODE=1234-5678\n"))
    assert s.api_key == "mf_api_pro_abc-DEF_123" and s.office_code == "1234-5678"


@pytest.mark.parametrize(
    "value,msg",
    [
        ("REPLACE_WITH_YOUR_API_KEY", "ダミー"),
        ("mf_api_prd_REPLACE_ME", "ダミー"),
        ("mf_api_prd_mf_api_pro_abc", "重複"),
        ("abc123", "mf_api_"),
    ],
)
def test_invalid_keys(tmp_path, value, msg):
    with pytest.raises(ConfigError, match=msg):
        load_settings(write_env(tmp_path, f"MF_API_KEY={value}\n"))


def test_dummy_office_code_is_treated_as_unset(tmp_path):
    s = load_settings(write_env(tmp_path, "MF_API_KEY=mf_api_pro_x\nMF_OFFICE_CODE=0000-0000\n"))
    assert s.office_code is None
    with pytest.raises(ConfigError):
        s.require_office_code()
