"""環境変数（.env）の読み込みと検証。"""

import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

OFFICE_CODE_RE = re.compile(r"^[0-9A-Za-z]{4}-[0-9A-Za-z]{4}$")
DUMMY_OFFICE_CODES = ("XXXX-XXXX", "0000-0000")


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    api_key: str
    office_code: str | None
    data_dir: Path
    journals_start_date: date | None
    journals_end_date: date | None
    journals_per_page: int
    transactions_per_page: int

    def require_office_code(self) -> str:
        if not self.office_code:
            raise ConfigError(
                "MF_OFFICE_CODE が未設定です。`python -m mf_accounting offices` で事業者番号を確認し、.env に設定してください。"
            )
        return self.office_code


def _parse_date(name: str) -> date | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as e:
        raise ConfigError(f"{name} は YYYY-MM-DD 形式で指定してください: {raw!r}") from e


def _parse_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError as e:
        raise ConfigError(f"{name} は整数で指定してください: {raw!r}") from e
    if not lo <= v <= hi:
        raise ConfigError(f"{name} は {lo}〜{hi} の範囲で指定してください: {v}")
    return v


def load_settings(env_file: str | os.PathLike | None = None) -> Settings:
    load_dotenv(env_file, override=False)

    api_key = os.getenv("MF_API_KEY", "").strip()
    if not api_key:
        raise ConfigError("MF_API_KEY が未設定です。.env.example を .env にコピーしてAPIキーを設定してください。")
    if "REPLACE" in api_key or api_key.startswith("mf_api_prd_xxxx"):
        raise ConfigError("MF_API_KEY がダミー値のままです。.env に実際のAPIキーを設定してください。")
    if api_key.count("mf_api_") > 1:
        raise ConfigError("MF_API_KEY の先頭 'mf_api_' が重複しています。発行されたキーそのものだけを設定してください。")
    if not api_key.startswith("mf_api_"):
        raise ConfigError("MF_API_KEY は 'mf_api_' で始まるAPIキーを設定してください。")

    office_code = os.getenv("MF_OFFICE_CODE", "").strip() or None
    if office_code is not None:
        if office_code in DUMMY_OFFICE_CODES:
            office_code = None
        elif not OFFICE_CODE_RE.match(office_code):
            raise ConfigError(f"MF_OFFICE_CODE は XXXX-XXXX 形式で指定してください: {office_code!r}")

    return Settings(
        api_key=api_key,
        office_code=office_code,
        data_dir=Path(os.getenv("MF_DATA_DIR", "").strip() or "data"),
        journals_start_date=_parse_date("MF_JOURNALS_START_DATE"),
        journals_end_date=_parse_date("MF_JOURNALS_END_DATE"),
        journals_per_page=_parse_int("MF_JOURNALS_PER_PAGE", 1000, 1, 10000),
        transactions_per_page=_parse_int("MF_TRANSACTIONS_PER_PAGE", 500, 10, 500),
    )
