"""全件取得時のデータ検証。想定外の構造・件数不一致があれば例外で停止する。

エラーメッセージには件数・フィールド名・IDのみを含め、摘要や金額などの値は含めない。
"""

from datetime import date

TRANSACTION_SIDES = {"INCOME", "EXPENSE"}
JOURNALIZING_STATUSES = {"excluded", "none", "registered", "modified", "new_voucher_attached"}


class DataValidationError(RuntimeError):
    pass


def _check_count_and_ids(kind: str, items: list[dict], meta: dict) -> None:
    total = meta.get("total_count")
    if total is None:
        raise DataValidationError(f"{kind}: metadata.total_count がありません")
    if len(items) != int(total):
        raise DataValidationError(f"{kind}: 取得件数 {len(items)} が total_count {total} と一致しません")
    ids = [i.get("id") for i in items]
    if any(not i for i in ids):
        raise DataValidationError(f"{kind}: id が空のレコードがあります")
    if len(set(ids)) != len(ids):
        raise DataValidationError(f"{kind}: ページ間でIDが重複しています（{len(ids) - len(set(ids))}件）")


def _in_range(d: str | None, start: date, end: date) -> bool:
    try:
        return start <= date.fromisoformat(str(d)[:10]) <= end
    except ValueError:
        return False


def validate_journals(items: list[dict], meta: dict, start: date, end: date) -> None:
    _check_count_and_ids("journals", items, meta)
    for j in items:
        jid = j.get("id")
        if not _in_range(j.get("transaction_date"), start, end):
            raise DataValidationError(f"journals: 期間外または不正な transaction_date（id={jid}）")
        branches = j.get("branches")
        if not isinstance(branches, list) or not branches:
            raise DataValidationError(f"journals: branches が空または配列ではありません（id={jid}）")
        for b in branches:
            if not isinstance(b, dict) or not (b.get("debitor") or b.get("creditor")):
                raise DataValidationError(f"journals: 借方・貸方の両方が空の明細行があります（id={jid}）")
            for side in (b.get("debitor"), b.get("creditor")):
                if side is not None and not isinstance(side.get("value"), int):
                    raise DataValidationError(f"journals: value が整数ではありません（id={jid}）")
        if j.get("transaction_id") is not None and not isinstance(j.get("transaction_id"), str):
            raise DataValidationError(f"journals: transaction_id の型が想定外です（id={jid}）")


def validate_transactions(items: list[dict], meta: dict, start: date, end: date) -> None:
    _check_count_and_ids("transactions", items, meta)
    for t in items:
        tid = t.get("id")
        if not _in_range(t.get("date"), start, end):
            raise DataValidationError(f"transactions: 期間外または不正な date（id={tid}）")
        if not isinstance(t.get("value"), int):
            raise DataValidationError(f"transactions: value が整数ではありません（id={tid}）")
        if t.get("side") not in TRANSACTION_SIDES:
            raise DataValidationError(f"transactions: 想定外の side（id={tid}）")
        if t.get("journalizing_status") not in JOURNALIZING_STATUSES:
            raise DataValidationError(f"transactions: 想定外の journalizing_status（id={tid}）")
        if not t.get("connected_sub_account_id"):
            raise DataValidationError(f"transactions: connected_sub_account_id が空です（id={tid}）")
