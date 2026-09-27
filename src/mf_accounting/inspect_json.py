"""取得したJSONの構造を「値を表示せずに」要約する。

Step 3/4 の確認用。フィールドごとの型・値あり件数と、個人情報らしきパターンの該当件数だけを出す。
"""

import re
from collections import defaultdict
from typing import Any

PII_PATTERNS = {
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "phone": re.compile(r"0\d{1,4}-\d{1,4}-\d{3,4}|\b0[5789]0\d{8}\b|\b0\d{9}\b"),
    "long_digits(7+)": re.compile(r"\d{7,}"),
}


def _type_name(v: Any) -> str:
    if v is None:
        return "null"
    return {bool: "bool", int: "int", float: "float", str: "str", list: "list", dict: "object"}.get(type(v), type(v).__name__)


def summarize(records: list[dict], date_field: str | None = None) -> dict:
    fields: dict[str, dict] = defaultdict(lambda: {"types": set(), "present": 0, "non_empty": 0, "pii": defaultdict(int)})

    def walk(obj: Any, path: str) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, f"{path}.{k}" if path else k)
            return
        f = fields[path]
        f["types"].add(_type_name(obj))
        f["present"] += 1
        if obj not in (None, "", [], {}):
            f["non_empty"] += 1
        if isinstance(obj, list):
            for item in obj:
                if isinstance(item, dict):
                    walk(item, path + "[]")
                else:
                    fields[path + "[]"]["types"].add(_type_name(item))
        if isinstance(obj, str):
            for name, pat in PII_PATTERNS.items():
                if pat.search(obj):
                    f["pii"][name] += 1

    for r in records:
        walk(r, "")

    dates = sorted(str(r[date_field]) for r in records if date_field and r.get(date_field))
    return {
        "count": len(records),
        "date_min": dates[0] if dates else None,
        "date_max": dates[-1] if dates else None,
        "fields": {
            p: {"types": sorted(f["types"]), "present": f["present"], "non_empty": f["non_empty"], "pii_matches": dict(f["pii"])}
            for p, f in sorted(fields.items())
        },
    }


def value_counts(records: list[dict], field: str) -> dict[str, int]:
    """列挙型フィールド（journal_type 等）の値の分布。自由記述フィールドには使わないこと。"""
    counts: dict[str, int] = defaultdict(int)
    for r in records:
        counts[str(r.get(field))] += 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def format_summary(summary: dict, enum_counts: dict[str, dict[str, int]] | None = None) -> str:
    lines = [f"件数: {summary['count']}", f"期間: {summary['date_min']} 〜 {summary['date_max']}", "", "フィールド（型 / 値あり件数 / 個人情報パターン該当件数）:"]
    for path, f in summary["fields"].items():
        pii = ", ".join(f"{k}={v}" for k, v in f["pii_matches"].items())
        lines.append(f"  {path:55s} {'/'.join(f['types']):18s} {f['non_empty']:>6}/{f['present']:<6} {pii}")
    for field, counts in (enum_counts or {}).items():
        lines.append("")
        lines.append(f"{field} の分布: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    return "\n".join(lines)
