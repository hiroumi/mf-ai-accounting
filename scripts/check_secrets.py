#!/usr/bin/env python3
"""コミット前に認証情報・取得データが含まれていないか検査する。

  python scripts/check_secrets.py            # ステージ済みの内容を検査（pre-commit フック用）
  python scripts/check_secrets.py --tracked  # Git管理下の全ファイルを検査

検査内容:
  - 禁止パス: .env（.env.example 以外）, data/ 配下（.gitkeep 以外）, *.pem, *.key
  - APIキー形式（mf_api_xxx_...）、JWT形式（eyJ...）
  - ローカル .env に設定された実際の MF_API_KEY / MF_OFFICE_CODE の値そのもの
"""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_PATH = [
    re.compile(r"(^|/)\.env($|\.(?!example$).*)"),
    re.compile(r"^data/(?!\.gitkeep$)"),
    re.compile(r"\.(pem|key)$"),
]
SECRET_PATTERNS = {
    "APIキー": re.compile(r"mf_api_[a-z]{2,5}_[A-Za-z0-9_-]{16,}"),
    "Anthropic APIキー": re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"),
    "JWT": re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
}
# ドキュメント中の公式サンプル値は除外
ALLOWED_LITERALS = {"mf_api_prd_a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6", "mf_api_prd_xxxxxxxxxxxxxxxx"}


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout


def local_env_values() -> dict[str, str]:
    env = ROOT / ".env"
    values = {}
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                v = v.strip().strip('"').strip("'")
                if k.strip() in ("MF_API_KEY", "MF_OFFICE_CODE", "ANTHROPIC_API_KEY") and v and "REPLACE" not in v and v != "0000-0000":
                    values[k.strip()] = v
    return values


def main() -> int:
    tracked = "--tracked" in sys.argv
    if tracked:
        paths = git("ls-files").splitlines()
        read = lambda p: (ROOT / p).read_bytes() if (ROOT / p).exists() else b""
    else:
        paths = git("diff", "--cached", "--name-only", "--diff-filter=ACMR").splitlines()
        read = lambda p: subprocess.run(["git", "show", f":{p}"], cwd=ROOT, capture_output=True).stdout

    real_values = local_env_values()
    problems = []
    for p in paths:
        if any(r.search(p) for r in FORBIDDEN_PATH):
            problems.append(f"{p}: コミット禁止のパスです")
            continue
        text = read(p).decode("utf-8", errors="ignore")
        for label, pat in SECRET_PATTERNS.items():
            for m in pat.finditer(text):
                if m.group(0) not in ALLOWED_LITERALS:
                    problems.append(f"{p}: {label}らしき文字列を検出しました")
                    break
        for k, v in real_values.items():
            if v in text:
                problems.append(f"{p}: .env の {k} の実際の値が含まれています")

    if problems:
        print("秘密情報チェックで問題が見つかりました（値は表示しません）:", file=sys.stderr)
        for pr in problems:
            print(f"  - {pr}", file=sys.stderr)
        return 1
    print(f"秘密情報チェックOK（{len(paths)}ファイル）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
