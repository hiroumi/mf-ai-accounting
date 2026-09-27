"""取得データのローカル保存（data/ 配下。Git管理対象外）。

data/raw/{office_code}/{kind}/{YYYYmmdd-HHMMSS}[-sample]/
    manifest.json   取得条件・件数・取得日時
    *.json          APIレスポンス
data/processed/{office_code}/*.csv
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any


def new_run_dir(data_dir: Path, office_code: str, kind: str, *, sample: bool = False) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    d = data_dir / "raw" / office_code / kind / (stamp + ("-sample" if sample else ""))
    d.mkdir(parents=True, exist_ok=False)
    return d


def latest_run_dir(data_dir: Path, office_code: str, kind: str) -> Path | None:
    base = data_dir / "raw" / office_code / kind
    if not base.is_dir():
        return None
    runs = sorted((p for p in base.iterdir() if p.is_dir() and _run_ok(p)), reverse=True)
    return runs[0] if runs else None


def _run_ok(run: Path) -> bool:
    """manifest があり、失敗として記録されていない取得ディレクトリのみ対象にする。"""
    m = run / "manifest.json"
    if not m.exists():
        return False
    try:
        return load_json(m).get("status") != "failed"
    except ValueError:
        return False


def processed_dir(data_dir: Path, office_code: str) -> Path:
    d = data_dir / "processed" / office_code
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_json(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)
    return path


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_manifest(run_dir: Path, **fields: Any) -> Path:
    return save_json(run_dir / "manifest.json", {"fetched_at": datetime.now().isoformat(timespec="seconds"), **fields})
