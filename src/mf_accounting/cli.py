"""コマンドライン入口: python -m mf_accounting <command>

  offices        Step 1: 認証と accessible_offices の取得
  masters        Step 2: 事業者情報・会計期間・マスターの取得
  counts         各会計期間の仕訳件数のみ確認（各期1件だけ取得し、内容は保存しない）
  journals       Step 3/6: 仕訳の取得（--sample N で少量 / --all で全期間）
  transactions   Step 4/6: 連携明細の取得（--sample N で少量 / --all で全期間）
  inspect        取得済みJSONの構造要約（値は表示しない）
  csv            Step 5: CSV変換
"""

import argparse
import json
import logging
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from . import endpoints as ep
from .auth import AuthError, TokenProvider
from .client import MFAccountingClient, MFApiError
from .config import ConfigError, Settings, load_settings
from .guard import ForbiddenRequestError, GuardedSession
from .inspect_json import format_summary, summarize, value_counts
from . import phase2, phase3, phase4, phase25, routing
from .quality import format_report, quality_report
from .validate import DataValidationError, validate_journals, validate_transactions
from .storage import latest_run_dir, load_json, new_run_dir, processed_dir, save_json, write_manifest
from .transform import (
    JOURNAL_LINE_COLUMNS,
    TRAINING_PAIR_COLUMNS,
    TRANSACTION_COLUMNS,
    flatten,
    journal_lines,
    training_pairs,
    transaction_rows,
    write_csv,
)

logger = logging.getLogger("mf_accounting")


def make_client(settings: Settings, *, office_code: str | None) -> MFAccountingClient:
    session = GuardedSession()
    session.headers["User-Agent"] = "mf-ai-accounting/0.1 (read-only)"
    tokens = TokenProvider(settings.api_key, session)
    return MFAccountingClient(tokens, session, office_code=office_code)


# ---- Step 1 ---------------------------------------------------------------


def cmd_offices(settings: Settings, args) -> None:
    client = make_client(settings, office_code=None)
    body = ep.accessible_offices(client)
    offices = body.get("accessible_offices") or []

    run = new_run_dir(settings.data_dir, "_all", "accessible_offices")
    save_json(run / "accessible_offices.json", body)
    write_manifest(run, endpoint="/api/v3/accessible_offices", count=len(offices))

    print(f"アクセス可能な事業者: {len(offices)}件")
    for o in offices:
        periods = o.get("accounting_periods") or []
        span = f"{periods[-1]['start_date']}〜{periods[0]['end_date']}" if periods else "-"
        print(f"  code={o.get('code')}  type={o.get('type')}  会計期間数={len(periods)} ({span})  name={o.get('name')}")
    if settings.office_code and settings.office_code not in {o.get("code") for o in offices}:
        print(f"警告: .env の MF_OFFICE_CODE={settings.office_code} はアクセス可能な事業者に含まれていません。")
    print(f"保存先: {run}")


# ---- Step 2 ---------------------------------------------------------------


def cmd_masters(settings: Settings, args) -> None:
    office_code = settings.require_office_code()
    client = make_client(settings, office_code=office_code)
    run = new_run_dir(settings.data_dir, office_code, "masters")

    results = {}
    for name, path, key in ep.MASTER_ENDPOINTS:
        try:
            body = client.get(path)
        except MFApiError as e:
            if e.status in (403, 404):
                logger.warning("%s を取得できませんでした（HTTP %s）: %s", name, e.status, e.errors)
                results[name] = {"path": path, "error": e.status}
                continue
            raise
        save_json(run / f"{name}.json", body)
        results[name] = {"path": path, "count": len(body.get(key) or []) if key else 1}

    write_manifest(run, office_code=office_code, endpoints=results)
    print(f"事業者 {office_code} のマスターを取得しました:")
    for name, r in results.items():
        print(f"  {name:20s} " + (f"{r['count']}件" if "count" in r else f"取得失敗 HTTP {r['error']}（権限を確認してください）"))
    print(f"保存先: {run}")


def _load_terms(settings: Settings, client: MFAccountingClient) -> list[dict]:
    masters = latest_run_dir(settings.data_dir, client.office_code, "masters")
    if masters and (masters / "term_settings.json").exists():
        return load_json(masters / "term_settings.json").get("term_settings") or []
    return ep.term_settings(client)


def _default_sample_range(terms: list[dict]) -> tuple[date, date]:
    """開始済みの最新の会計期間（今日を上限）。"""
    today = date.today()
    started = [t for t in terms if date.fromisoformat(t["start_date"]) <= today]
    if not started:
        raise ConfigError("会計期間が取得できませんでした。--start / --end を指定してください。")
    t = max(started, key=lambda t: t["start_date"])
    return date.fromisoformat(t["start_date"]), min(date.fromisoformat(t["end_date"]), today)


# ---- Step 3 / 6: journals ---------------------------------------------------


def cmd_counts(settings: Settings, args) -> None:
    """各期の仕訳総件数を metadata.total_count から確認する。仕訳の内容は保存・表示しない。"""
    office_code = settings.require_office_code()
    client = make_client(settings, office_code=office_code)
    periods = ep.periods_from_term_settings(_load_terms(settings, client))
    rows = []
    for p in periods:
        items, meta = ep.journals(client, p["start_date"], p["end_date"], per_page=1, max_items=1)
        total = int(meta["total_count"] or 0)
        first_kind = items[0].get("entered_by") if items else None
        rows.append({"fiscal_year": p["fiscal_year"], "start_date": str(p["start_date"]), "end_date": str(p["end_date"]),
                     "total_count": total, "first_entered_by": first_kind})
    run = new_run_dir(settings.data_dir, office_code, "journal_counts")
    write_manifest(run, office_code=office_code, periods=rows)
    print(f"{'会計期間':26s} {'仕訳総件数':>8s}  1件目の種類")
    for r in rows:
        print(f"FY{r['fiscal_year']} {r['start_date']}〜{r['end_date']} {r['total_count']:>8}  {r['first_entered_by']}")
    print(f"合計 {sum(r['total_count'] for r in rows)}件  保存先: {run}")



def cmd_journals(settings: Settings, args) -> None:
    office_code = settings.require_office_code()
    client = make_client(settings, office_code=office_code)

    if args.all:
        terms = _load_terms(settings, client)
        periods = ep.periods_from_term_settings(terms, settings.journals_start_date, settings.journals_end_date)
        run = new_run_dir(settings.data_dir, office_code, "journals")
        summary = []
        try:
            for p in periods:
                items, meta = ep.journals(client, p["start_date"], p["end_date"], per_page=settings.journals_per_page)
                fname = f"journals_{p['start_date']}_{p['end_date']}.json"
                save_json(run / fname, {"period": p, "metadata": meta, "journals": items})
                validate_journals(items, meta, p["start_date"], p["end_date"])
                summary.append({"file": fname, **{k: str(v) for k, v in p.items()}, **meta})
                print(f"  FY{p['fiscal_year']} {p['start_date']}〜{p['end_date']}: {len(items)}件 (total_count={meta['total_count']}, pages={meta['pages_fetched']})")
        except Exception as e:
            write_manifest(run, office_code=office_code, mode="all", status="failed", error=str(e), periods=summary)
            raise
        write_manifest(run, office_code=office_code, mode="all", status="complete", periods=summary)
        print(f"合計 {sum(s['fetched_count'] for s in summary)}件  保存先: {run}")
        return

    start, end = _resolve_sample_range(settings, client, args)
    # 開始仕訳を除外する場合は少し多めに1ページ取得して除外後に N 件へ切り詰める
    fetch_n = args.sample + 5 if args.exclude_opening else args.sample
    items, meta = ep.journals(client, start, end, per_page=fetch_n, max_items=fetch_n)
    excluded = 0
    if args.exclude_opening:
        kept = [j for j in items if j.get("entered_by") != ep.OPENING_ENTERED_BY]
        excluded = len(items) - len(kept)
        items = kept[: args.sample]
        meta["fetched_count"] = len(items)
    run = new_run_dir(settings.data_dir, office_code, "journals", sample=True)
    save_json(run / f"journals_{start}_{end}.json", {"period": {"start_date": start, "end_date": end}, "metadata": meta, "journals": items})
    write_manifest(run, office_code=office_code, mode="sample", start_date=start, end_date=end, excluded_opening=excluded, **meta)
    print(f"少量取得: 条件 {start}〜{end} / 取得 {len(items)}件（開始仕訳除外 {excluded}件） / 条件に合う総件数 {meta['total_count']}件")
    print(f"保存先: {run}")


# ---- Step 4 / 6: transactions -------------------------------------------------


def cmd_transactions(settings: Settings, args) -> None:
    office_code = settings.require_office_code()
    client = make_client(settings, office_code=office_code)

    if args.all:
        terms = _load_terms(settings, client)
        periods = ep.periods_from_term_settings(terms, settings.journals_start_date, settings.journals_end_date)
        if not periods:
            raise ConfigError("取得対象の会計期間がありません。")
        run = new_run_dir(settings.data_dir, office_code, "transactions")
        summary = []
        try:
            # 会計期間ごと（各期は366日以内）に取得し、年度別に集計しやすくする
            for p in periods:
                for w_start, w_end in ep.split_date_range(p["start_date"], min(p["end_date"], date.today())):
                    items, meta = ep.transactions(client, w_start, w_end, per_page=settings.transactions_per_page)
                    fname = f"transactions_{w_start}_{w_end}.json"
                    save_json(run / fname, {"period": {"start_date": w_start, "end_date": w_end}, "metadata": meta, "transactions": items})
                    validate_transactions(items, meta, w_start, w_end)
                    summary.append({"file": fname, "fiscal_year": p["fiscal_year"], "start_date": str(w_start), "end_date": str(w_end), **meta})
                    print(f"  FY{p['fiscal_year']} {w_start}〜{w_end}: {len(items)}件 (total_count={meta['total_count']}, pages={meta['pages_fetched']})")
        except Exception as e:
            write_manifest(run, office_code=office_code, mode="all", status="failed", error=str(e), windows=summary)
            raise
        write_manifest(run, office_code=office_code, mode="all", status="complete", windows=summary)
        print(f"合計 {sum(s['fetched_count'] for s in summary)}件  保存先: {run}")
        return

    start, end = _resolve_sample_range(settings, client, args)
    if (end - start).days > ep.TRANSACTIONS_MAX_SPAN_DAYS:
        start = end - timedelta(days=365)
    per_page = min(max(args.sample, 10), 500)  # 仕様: per_page は 10〜500
    items, meta = ep.transactions(client, start, end, per_page=per_page, max_items=args.sample)
    run = new_run_dir(settings.data_dir, office_code, "transactions", sample=True)
    save_json(run / f"transactions_{start}_{end}.json", {"period": {"start_date": start, "end_date": end}, "metadata": meta, "transactions": items})
    write_manifest(run, office_code=office_code, mode="sample", start_date=start, end_date=end, **meta)
    print(f"少量取得: 条件 {start}〜{end} / 取得 {len(items)}件 / 条件に合う総件数 {meta['total_count']}件")
    print(f"保存先: {run}")


def _resolve_sample_range(settings: Settings, client: MFAccountingClient, args) -> tuple[date, date]:
    if args.start or args.end:
        if not (args.start and args.end):
            raise ConfigError("--start と --end は両方指定してください。")
        return date.fromisoformat(args.start), date.fromisoformat(args.end)
    return _default_sample_range(_load_terms(settings, client))


def cmd_linkcheck(settings: Settings, args) -> None:
    """明細IDで仕訳を検索し（GET /journals?transaction_ids=...）、紐付けを確認する。値は表示しない。"""
    office_code = settings.require_office_code()
    run = _run_or_latest(settings, office_code, "transactions", args.run)
    if run is None:
        raise ConfigError("transactions の取得データがありません。")
    txs = _load_records(run, "transactions")[: ep.TRANSACTION_IDS_MAX]
    if not txs:
        raise ConfigError("明細が0件のため確認できません。")
    manifest = load_json(run / "manifest.json")
    start, end = date.fromisoformat(manifest["start_date"]), date.fromisoformat(manifest["end_date"])
    client = make_client(settings, office_code=office_code)
    items, meta = ep.journals(client, start, end, per_page=100, transaction_ids=[t["id"] for t in txs])
    save_json(run / "linked_journals.json", {"metadata": meta, "journals": items})

    tx_ids = {t["id"] for t in txs}
    by_tx: dict[str, int] = {}
    for j in items:
        by_tx[j.get("transaction_id")] = by_tx.get(j.get("transaction_id"), 0) + 1
    print(f"対象明細: {len(txs)}件（{start}〜{end}）")
    print("明細の仕訳化ステータス: " + ", ".join(f"{k}={v}" for k, v in value_counts(txs, "journalizing_status").items()))
    print(f"transaction_ids で返った仕訳: {len(items)}件")
    print(f"  うち transaction_id が対象明細IDと一致: {sum(1 for j in items if j.get('transaction_id') in tx_ids)}件")
    print(f"  紐付いた明細の数: {len(set(by_tx) & tx_ids)} / {len(txs)}  1明細あたりの仕訳数: {sorted(set(by_tx.values()))}")
    st = {t["id"]: t.get("journalizing_status") for t in txs}
    print("  ステータス別の紐付き: " + ", ".join(
        f"{k}={sum(1 for i in tx_ids if st[i] == k and i in by_tx)}/{sum(1 for i in tx_ids if st[i] == k)}"
        for k in sorted(set(st.values()), key=str)))
    print(f"保存先: {run / 'linked_journals.json'}")


# ---- inspect / csv ----------------------------------------------------------


def _load_records(run: Path, key: str) -> list[dict]:
    seen, out = set(), []
    for f in sorted(run.glob(f"{key}_*.json")):
        for r in load_json(f).get(key) or []:
            if r.get("id") not in seen:
                seen.add(r.get("id"))
                out.append(r)
    return out


def _run_or_latest(settings: Settings, office_code: str, kind: str, override: str | None) -> Path | None:
    return Path(override) if override else latest_run_dir(settings.data_dir, office_code, kind)


def cmd_inspect(settings: Settings, args) -> None:
    office_code = settings.require_office_code()
    run = _run_or_latest(settings, office_code, args.kind, args.run)
    if run is None:
        raise ConfigError(f"{args.kind} の取得データがありません。")
    records = _load_records(run, args.kind)
    if args.kind == "journals":
        s = summarize(records, "transaction_date")
        enums = {f: value_counts(records, f) for f in ("journal_type", "entered_by", "is_realized")}
        enums["branches数"] = value_counts([{"n": len(r.get("branches") or [])} for r in records], "n")
        sides = [side for r in records for b in r.get("branches") or [] for side in (b.get("debitor"), b.get("creditor")) if side]
        # 税区分名はマスターの区分名であり取引固有の値ではないため表示してよい
        enums["税区分(tax_name)"] = value_counts([{"t": s.get("tax_name") or "(空)"} for s in sides], "t")
        enums["invoice_kind"] = value_counts(sides, "invoice_kind")
        enums["tax_value>0 の借方/貸方"] = value_counts([{"v": (s.get("tax_value") or 0) > 0} for s in sides], "v")
        enums["transaction_idあり"] = value_counts([{"v": bool(r.get("transaction_id"))} for r in records], "v")
    else:
        s = summarize(records, "date")
        enums = {f: value_counts(records, f) for f in ("side", "journalizing_status")}
    print(f"対象: {run}")
    print(format_summary(s, enums))


def _dedupe(records: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in records:
        if r.get("id") not in seen:
            seen.add(r.get("id"))
            out.append(r)
    return out


def cmd_csv(settings: Settings, args) -> None:
    office_code = settings.require_office_code()
    out = processed_dir(settings.data_dir, office_code)
    sources: dict = {}

    masters = _run_or_latest(settings, office_code, "masters", None)
    connected, terms = [], []
    if masters:
        for name, _, key in ep.MASTER_ENDPOINTS:
            f = masters / f"{name}.json"
            if not f.exists():
                continue
            body = load_json(f)
            rows = [flatten(r) for r in (body.get(key) or [])] if key else [flatten(body)]
            write_csv(out / f"{name}.csv", rows)
            if name == "connected_accounts":
                connected = body.get(key) or []
            if name == "term_settings":
                terms = body.get(key) or []
        sources["masters"] = str(masters)

    journals: list[dict] = []
    jrun = _run_or_latest(settings, office_code, "journals", args.journals_run)
    if jrun:
        journals += _load_records(jrun, "journals")
        sources["journals"] = str(jrun)
    transactions: list[dict] = []
    trun = _run_or_latest(settings, office_code, "transactions", args.transactions_run)
    if trun:
        transactions = _load_records(trun, "transactions")
        sources["transactions"] = str(trun)
        linked = trun / "linked_journals.json"  # linkcheck で明細IDから取得した仕訳
        if linked.exists():
            journals += load_json(linked).get("journals") or []
            sources["linked_journals"] = str(linked)
    journals = _dedupe(journals)

    jl_rows = journal_lines(journals, terms)
    write_csv(out / "journal_lines.csv", jl_rows, JOURNAL_LINE_COLUMNS)

    linked_counts: dict[str, int] = {}
    for j in journals:
        if j.get("transaction_id"):
            linked_counts[j["transaction_id"]] = linked_counts.get(j["transaction_id"], 0) + 1
    tx_rows = transaction_rows(transactions, connected, terms, linked_counts)
    write_csv(out / "transactions.csv", tx_rows, TRANSACTION_COLUMNS)

    pairs, stats = training_pairs(tx_rows, journals, terms)
    write_csv(out / "training_pairs.csv", pairs, TRAINING_PAIR_COLUMNS)

    save_json(out / "manifest.json", {"sources": sources, "journals": len(journals), "journal_lines": len(jl_rows), "transactions": len(tx_rows), "training_pairs": stats})
    print(f"CSVを出力しました: {out}")
    print(f"  journal_lines.csv: {len(jl_rows)}行（仕訳 {len(journals)}件） / transactions.csv: {len(tx_rows)}行 / training_pairs.csv: {len(pairs)}行")
    print(f"  明細→仕訳の紐付け: {stats}")


def cmd_report(settings: Settings, args) -> None:
    """年度別・全体の品質集計（件数・率のみ。値は表示しない）。"""
    office_code = settings.require_office_code()
    masters = _run_or_latest(settings, office_code, "masters", None)
    jrun = _run_or_latest(settings, office_code, "journals", args.journals_run)
    trun = _run_or_latest(settings, office_code, "transactions", args.transactions_run)
    if not (masters and jrun and trun):
        raise ConfigError("masters / journals / transactions の取得データが必要です。")
    terms = load_json(masters / "term_settings.json").get("term_settings") or []
    connected = load_json(masters / "connected_accounts.json").get("connected_accounts") or []
    fys = list(range(args.from_fy, args.to_fy + 1))
    report = quality_report(_load_records(jrun, "journals"), _load_records(trun, "transactions"), terms, connected, fys)
    out = processed_dir(settings.data_dir, office_code)
    save_json(out / "quality_report.json", {"sources": {"journals": str(jrun), "transactions": str(trun)}, **report})
    print(f"仕訳: {jrun}\n明細: {trun}\n")
    print(format_report(report))
    print(f"\n保存先: {out / 'quality_report.json'}")


def _pct(x) -> str:
    return "-" if x is None else f"{x * 100:.1f}%"


def cmd_analyze(settings: Settings, args) -> None:
    """Phase 2 準備: 過去データのみでの相手科目推定の評価。件数・率・分布のみ表示する。"""
    from collections import Counter

    office_code = settings.require_office_code()
    masters = _run_or_latest(settings, office_code, "masters", None)
    jrun = _run_or_latest(settings, office_code, "journals", args.journals_run)
    trun = _run_or_latest(settings, office_code, "transactions", args.transactions_run)
    if not (masters and jrun and trun):
        raise ConfigError("masters / journals / transactions の取得データが必要です。")
    terms = load_json(masters / "term_settings.json").get("term_settings") or []
    connected = load_json(masters / "connected_accounts.json").get("connected_accounts") or []
    rows, bank_map = phase2.build_labels(_load_records(jrun, "journals"), _load_records(trun, "transactions"), terms, connected)
    rows = [r for r in rows if r["fiscal_year"] is not None and args.from_fy <= r["fiscal_year"] <= args.to_fy]

    out = processed_dir(settings.data_dir, office_code) / "phase2"
    write_csv(out / "counter_labels.csv", rows)
    bank_rows = [
        {"fiscal_year": k[0], "connected_sub_account_id": k[1], "account_id": v["key"][0] if v["key"] else None,
         "sub_account_id": v["key"][1] if v["key"] else None, "support": v["support"], "journals": v["journals"], "ratio": round(v["ratio"], 4)}
        for k, v in sorted(bank_map.by_sub.items(), key=lambda kv: (kv[0][0] or 0, str(kv[0][1])))
    ]
    write_csv(out / "bank_account_map.csv", bank_rows)
    fys = list(range(args.from_fy, args.to_fy + 1))
    report: dict = {"sources": {"journals": str(jrun), "transactions": str(trun)}}

    # 1. 口座側
    print("## 1. 口座側の推定")
    print("| FY | 紐付き明細 | 期×口座の推定 | 期×サービス | 金額から一意 | 現マスター | 未解決 | 口座側純額=明細金額 |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|")
    bank_stats = {}
    for fy in fys + ["合計"]:
        rs = rows if fy == "合計" else [r for r in rows if r["fiscal_year"] == fy]
        m = Counter(r["bank_method"] for r in rs)
        ok = sum(1 for r in rs if r["bank_amount_matches_tx"])
        bank_stats[str(fy)] = {"n": len(rs), "methods": dict(m), "amount_match": ok}
        print(f"| {fy} | {len(rs)} | {m['fy_sub_account_map']} | {m['fy_service_map']} | {m['journal_amount']} | {m['current_master']} | {m['unresolved']} | {ok} ({_pct(ok / len(rs) if rs else None)}) |")
    diff = [b for b in bank_rows if b["account_id"] and b["support"] >= phase2.MIN_MAP_SUPPORT]
    master = {c.get("id"): (c.get("account_id"), c.get("sub_account_id")) for a in connected for c in a.get("connected_sub_accounts") or []}
    n_diff = sum(1 for b in diff if (b["account_id"], b["sub_account_id"]) != master.get(b["connected_sub_account_id"]))
    print(f"期×口座の推定 {len(diff)}組のうち、現マスターと異なる科目: {n_diff}組")
    report["bank"] = {"by_fy": bank_stats, "maps": len(diff), "maps_differ_from_master": n_diff}

    # 2. 相手科目
    labeled = [r for r in rows if r.get("label_account")]
    cc = Counter(r["counter_count"] for r in labeled)
    print("\n## 2. 相手科目の抽出")
    print(f"ラベルあり {len(labeled)}/{len(rows)}件、複合（相手科目2つ以上） {sum(1 for r in labeled if r['complex_journal'])}件"
          f"（{_pct(sum(1 for r in labeled if r['complex_journal']) / len(labeled))}）、マイナスの相手科目を含む {sum(1 for r in labeled if r['has_negative_counter'])}件")
    print(f"相手科目数の分布: {dict(sorted(cc.items()))}")
    print(f"ラベルの種類数: 科目 {len({r['label_account'] for r in labeled})} / 科目+補助科目 {len({r['label_account_sub'] for r in labeled})} / 税区分 {len({r['label_tax'] for r in labeled})}")
    report["labels"] = {"labeled": len(labeled), "rows": len(rows), "counter_count": dict(cc)}

    # 3. exact content
    stats = phase2.content_stats(labeled)
    write_csv(out / "content_stats.csv", [{**s, "labels": s["labels"], "labels_by_fy": s["labels_by_fy"]} for s in stats])
    n_rows = sum(s["count"] for s in stats)
    bucket = lambda n: "1回" if n == 1 else "2-4回" if n < 5 else "5-9回" if n < 10 else "10回以上"
    cb = Counter(bucket(s["count"]) for s in stats)
    cb_rows = Counter()
    for s in stats:
        cb_rows[bucket(s["count"])] += s["count"]
    multi = [s for s in stats if s["distinct_labels"] > 1]
    rep = [s for s in stats if s["count"] > 1]
    tr = Counter("100%" if s["top_ratio"] == 1 else "90-99%" if s["top_ratio"] >= 0.9 else "70-89%" if s["top_ratio"] >= 0.7 else "50-69%" if s["top_ratio"] >= 0.5 else "50%未満" for s in rep)
    print("\n## 3. exact content 分析（科目レベル, FY%d〜FY%d）" % (args.from_fy, args.to_fy))
    print(f"content の種類 {len(stats)}（明細 {n_rows}件）")
    print("出現回数別（種類 / 明細件数）: " + ", ".join(f"{k}: {cb[k]} / {cb_rows[k]}" for k in ("1回", "2-4回", "5-9回", "10回以上")))
    print(f"複数科目に分類された content: {len(multi)}/{len(rep)}種類（2回以上出現のうち {_pct(len(multi) / len(rep) if rep else None)}）、"
          f"該当明細 {sum(s['count'] for s in multi)}件（{_pct(sum(s['count'] for s in multi) / n_rows)}）")
    print("最頻科目の比率（2回以上出現の content）: " + ", ".join(f"{k}={tr[k]}" for k in ("100%", "90-99%", "70-89%", "50-69%", "50%未満")))
    print(f"直近科目 = 最頻科目: {sum(1 for s in rep if s['latest_label'] == s['top_label'])}/{len(rep)}種類")
    fy_span = Counter(len(s["labels_by_fy"]) for s in rep)
    fy_change = sum(1 for s in rep if len(s["labels_by_fy"]) > 1 and len({max(v, key=v.get) for v in s["labels_by_fy"].values()}) > 1)
    print(f"出現した年度数の分布: {dict(sorted(fy_span.items()))}、年度により最頻科目が変わった content: {fy_change}種類")
    report["content"] = {"distinct": len(stats), "rows": n_rows, "by_count": dict(cb), "rows_by_count": dict(cb_rows),
                         "multi_label": len(multi), "repeated": len(rep), "top_ratio": dict(tr), "fy_change": fy_change}

    # 4/5. バックテスト
    report["backtest"] = {}
    for label, title in (("label_account", "科目"), ("label_account_sub", "科目+補助科目")):
        print(f"\n## 4. 時系列バックテスト（exact content, {title}）")
        print("| 評価年度 | 教師データ | 評価対象 | 過去に同一contentあり | 最頻科目の正解率 | 直近科目の正解率 | 最頻科目の正解率(全体比) |")
        print("|---|---|---:|---:|---:|---:|---:|")
        for fy in args.eval_fy:
            b = phase2.backtest(rows, fy, label)
            report["backtest"][f"{label}:{fy}"] = b
            print(f"| FY{fy} | FY{b['train_fys'][0]}〜FY{b['train_fys'][-1]} | {b['test']} | {b['covered']} ({_pct(b['coverage'])}) | "
                  f"{_pct(b['mode_accuracy_on_covered'])} | {_pct(b['latest_accuracy_on_covered'])} | {_pct(b['mode_correct_of_all_test'])} |")

    print("\n## 5. confidence 候補（過去の同一contentがすべて同じ科目）")
    for label, title in (("label_account", "科目"), ("label_account_sub", "科目+補助科目")):
        print(f"\n{title}レベル")
        print("| 条件 | " + " | ".join(f"FY{fy} 対象件数(比率) / 正解率" for fy in args.eval_fy) + " |")
        print("|---|" + "---:|" * len(args.eval_fy))
        for n in (10, 5, 3, 2, 1):
            cells = []
            for fy in args.eval_fy:
                c = report["backtest"][f"{label}:{fy}"]["confidence_unanimous"][f">={n}"]
                cells.append(f"{c['n']} ({_pct(c['share_of_test'])}) / {_pct(c['accuracy'])}")
            print(f"| 過去{n}回以上・100%同一 | " + " | ".join(cells) + " |")

    save_json(out / "phase2_report.json", report)
    print(f"\n派生データ: {out}（counter_labels.csv, bank_account_map.csv, content_stats.csv, phase2_report.json）")


def cmd_analyze25(settings: Settings, args) -> None:
    """Phase 2.5: 直近重視・年度重み・記帳方針変更検知の比較（件数・率のみ表示）。"""
    from collections import Counter

    office_code = settings.require_office_code()
    masters = _run_or_latest(settings, office_code, "masters", None)
    jrun = _run_or_latest(settings, office_code, "journals", None)
    trun = _run_or_latest(settings, office_code, "transactions", None)
    terms = load_json(masters / "term_settings.json").get("term_settings") or []
    connected = load_json(masters / "connected_accounts.json").get("connected_accounts") or []
    rows, _ = phase2.build_labels(_load_records(jrun, "journals"), _load_records(trun, "transactions"), terms, connected)
    rows = [r for r in rows if r["fiscal_year"] is not None and 2018 <= r["fiscal_year"] <= 2024]
    out = processed_dir(settings.data_dir, office_code) / "phase25"
    target = args.target_fy
    hist = phase25.histories(rows, target)
    report: dict = {"target_fy": target}

    # 1. Recent-window
    wins = [("全期間", None), ("直近10件", 10), ("直近5件", 5), ("直近3件", 3), ("直近2件", 2)]
    wrows = []
    for content, o in hist.items():
        row = {"content": content, "n": len(o)}
        for name, k in wins:
            st = phase25.window_stats(o, k)
            row.update({f"{name}_mode": st["mode"], f"{name}_agreement": round(st["agreement"], 4), f"{name}_changed": st["changed_in_window"]})
        row["last_date"] = o[-1].date
        wrows.append(row)
    write_csv(out / f"content_windows_before_fy{target}.csv", wrows)
    multi = [r for r in wrows if r["n"] >= 2]
    print(f"## 1. Recent-window（FY{target}評価時点 = FY{target - 1}末までの履歴, 2回以上出現 {len(multi)}種類）")
    print("| 窓 | 平均一致率 | 一致率100%の種類 | 窓内で科目変更あり | 最頻が全期間の最頻と異なる |")
    print("|---|---:|---:|---:|---:|")
    for name, _ in wins:
        avg = sum(r[f"{name}_agreement"] for r in multi) / len(multi)
        print(f"| {name} | {_pct(avg)} | {sum(1 for r in multi if r[f'{name}_agreement'] == 1)} | {sum(1 for r in multi if r[f'{name}_changed'])} | {sum(1 for r in multi if r[f'{name}_mode'] != r['全期間_mode'])} |")
    last_fy = Counter(o[-1].fy for o in hist.values())
    print("最終利用年度の分布（全content）: " + ", ".join(f"FY{k}={v}" for k, v in sorted(last_fy.items())))

    # 3. 記帳方針変更の検知
    test = [r for r in rows if r["fiscal_year"] == target and r.get("label_account") and r.get("tx_content")]
    print(f"\n## 3. 記帳方針変更の検知（FY{target - 1}末時点）")
    report["change"] = {}
    for m in (2, 3):
        ch = {c: phase25.detect_change(o, m) for c, o in hist.items()}
        det = {c for c, v in ch.items() if v["change_detected"]}
        trows = [r for r in test if r["tx_content"] in det]
        cur_ok = sum(1 for r in trows if ch[r["tx_content"]]["current_account"] == r["label_account"])
        prev_ok = sum(1 for r in trows if ch[r["tx_content"]]["previous_mode"] == r["label_account"])
        mode_ok = sum(1 for r in trows if phase25.mode(hist[r["tx_content"]])[0] == r["label_account"])
        since = Counter(min(ch[c]["observations_since_change"], 5) for c in det)
        print(f"連続{m}件以上で変更検知: {len(det)}/{sum(1 for o in hist.values() if len(o) >= m + 1)}種類。FY{target}の該当明細 {len(trows)}件で "
              f"変更後の科目が正解 {cur_ok} ({_pct(cur_ok / len(trows) if trows else None)}) / 変更前の最頻が正解 {prev_ok} / 全期間最頻が正解 {mode_ok}。"
              f"変更後の連続件数: {dict(sorted(since.items()))}（5=5件以上）")
        report["change"][m] = {"detected": len(det), "test_rows": len(trows), "current_correct": cur_ok, "previous_correct": prev_ok, "mode_correct": mode_ok}
        write_csv(out / f"content_changes_min{m}_before_fy{target}.csv", [{"content": c, **v} for c, v in ch.items()])
    # FY2024 で新たに起きた変更（評価年度内の出現で最頻が過去と変わった）
    new_change = 0
    for c, o in hist.items():
        now = [r["label_account"] for r in test if r["tx_content"] == c]
        if now and Counter(now).most_common(1)[0][0] != phase25.mode(o)[0]:
            new_change += 1
    print(f"FY{target}中に最頻科目が過去の最頻から変わった content: {new_change}種類（評価時点では未検知の変更）")

    # 4. simple / complex
    shift = phase25.structure_shift(rows, target)
    print(f"\n## 4. 構造の変化（過去 → FY{target}, 過去に同一contentありの明細）")
    print("明細: " + ", ".join(f"{k}={v}" for k, v in sorted(shift["rows"].items())) + " / content種類: " + ", ".join(f"{k}={v}" for k, v in sorted(shift["contents"].items())))
    report["structure_shift"] = shift

    # 5. 方式比較（年度単位 = 主評価 / ローリング = 実運用に近い補助評価）
    fys = sorted(set(args.eval_fy) | {2022, 2023, 2024})
    pairs = {(mode_, fy): (phase25.year_block_pairs if mode_ == "year" else phase25.rolling_pairs)(rows, fy) for mode_ in ("year", "rolling") for fy in fys}
    mode_title = {"year": "年度単位（教師: 評価年度より前の年度）", "rolling": "ローリング（各明細の取引日より前の全データ）"}
    report["compare"] = {}
    for mode_ in ("year", "rolling"):
        cmp = {fy: phase25.compare_methods(pairs[(mode_, fy)], fy) for fy in fys}
        report["compare"][mode_] = cmp
        v24 = cmp[target]
        print(f"\n## 5. 方式比較 FY{target} — {mode_title[mode_]}")
        print("| 方式 | coverage | accuracy | simple accuracy (件数) | complex accuracy (件数) | 参考: FY2023 / FY2022 accuracy |")
        print("|---|---:|---:|---:|---:|---:|")
        for name, v in v24.items():
            print(f"| {name} | {v['covered']}/{v['test']} ({_pct(v['coverage'])}) | {_pct(v['accuracy'])} | {_pct(v['simple_accuracy'])} ({v['simple_n']}) | "
                  f"{_pct(v['complex_accuracy'])} ({v['complex_n']}) | {_pct(cmp[2023][name]['accuracy'])} / {_pct(cmp[2022][name]['accuracy'])} |")

    # 6. High-confidence 条件
    grid = phase25.rule_grid()
    report["rules"] = {}
    for mode_ in ("year", "rolling"):
        res = {r: {fy: phase25.evaluate_rule(r, pairs[(mode_, fy)], fy) for fy in (2022, 2023, 2024)} for r in grid}
        report["rules"][mode_] = [{"rule": r.name(), **{str(fy): res[r][fy] for fy in (2022, 2023, 2024)}} for r in grid]
        def row(r, res=res):
            v = res[r]
            return f"| {r.name()} | " + " | ".join(f"{v[fy]['coverage'] * 100:.1f}% / {_pct(v[fy]['accuracy'])} ({v[fy]['n']})" for fy in (2022, 2023, 2024)) + " |"
        hdr = "| 条件 | FY2022 coverage / accuracy (件数) | FY2023 | FY2024 |\n|---|---:|---:|---:|"
        print(f"\n## 6. High-confidence 条件 — {mode_title[mode_]}（{len(grid)}通り）")
        print("\n### 代表的な条件")
        print(hdr)
        for r in [phase25.Rule(10, 10, "any", False, True), phase25.Rule(5, 5, "any", False, True), phase25.Rule(3, 3, "any", False, True),
                  phase25.Rule(2, 2, "any", False, False), phase25.Rule(3, 3, "any", False, False), phase25.Rule(5, 5, "any", False, False),
                  phase25.Rule(3, 3, "prev_fy", True, False), phase25.Rule(5, 5, "prev_fy", True, False), phase25.Rule(5, 10, "prev_fy", True, True)]:
            print(row(r))
        ok = lambda v, a=0.98: v["accuracy"] is not None and v["accuracy"] >= a and v["n"] >= 20
        fair = sorted([r for r in grid if ok(res[r][2022]) and ok(res[r][2023])], key=lambda r: -(res[r][2022]["coverage"] + res[r][2023]["coverage"]))
        print("\n### FY2022・FY2023 の両方で accuracy 98%以上（各20件以上）→ coverage 上位を FY2024 に適用（公正な評価）")
        print(hdr)
        for r in fair[:6]:
            print(row(r))
        if not fair:
            print("| 該当なし | | | |")
        print("\n### 参考: FY2024 の accuracy 下限ごとの最大 coverage（FY2024を見て選んだ楽観値）")
        for a in (0.95, 0.97, 0.98, 0.99):
            best = max((r for r in grid if ok(res[r][2024], a)), key=lambda r: res[r][2024]["coverage"], default=None)
            print(f"- accuracy ≥{a * 100:.0f}%: " + (f"coverage {res[best][2024]['coverage'] * 100:.1f}%（{res[best][2024]['n']}件, 実accuracy {_pct(res[best][2024]['accuracy'])}）: {best.name()}" if best else "該当なし"))
    save_json(out / "phase25_report.json", report)
    print(f"\n派生データ: {out}")


def cmd_analyze3(settings: Settings, args) -> None:
    """Phase 3: 正規化 + fuzzy matching による主科目推定のローリング評価（件数・率のみ表示）。"""
    office_code = settings.require_office_code()
    masters = _run_or_latest(settings, office_code, "masters", None)
    jrun = _run_or_latest(settings, office_code, "journals", None)
    trun = _run_or_latest(settings, office_code, "transactions", None)
    terms = load_json(masters / "term_settings.json").get("term_settings") or []
    connected = load_json(masters / "connected_accounts.json").get("connected_accounts") or []
    rows, _ = phase2.build_labels(_load_records(jrun, "journals"), _load_records(trun, "transactions"), terms, connected)
    rows = [r for r in rows if r["fiscal_year"] is not None and 2018 <= r["fiscal_year"] <= 2024]
    out = processed_dir(settings.data_dir, office_code) / "phase3"
    fys = (2022, 2023, 2024)
    thresholds = (95, 90, 85, 80, 75, 70)
    report: dict = {}

    preds = {(m, fy): phase3.rolling_predictions(rows, fy, m, "ratio") for m in phase3.NORMALIZERS for fy in fys}

    # 2. raw exact vs normalized exact
    print("## 2. raw exact vs normalized exact（ローリング, 主科目）")
    print("| 正規化 | " + " | ".join(f"FY{fy} exact計 coverage / accuracy（うち正規化で追加 件数 / accuracy）" for fy in fys) + " |")
    print("|---|" + "---:|" * len(fys))
    for m in phase3.NORMALIZERS:
        cells = []
        for fy in fys:
            s = phase3.stage_summary(phase3.apply_threshold(preds[(m, fy)], None))
            report[f"exact:{m}:{fy}"] = s
            add = s["norm_exact"]
            cells.append(f"{_pct(s['total']['coverage'])} / {_pct(s['total']['accuracy'])}（+{add['n']} / {_pct(add['accuracy'])}）")
        print(f"| {m} | " + " | ".join(cells) + " |")

    # 3. fuzzy: 閾値比較（追加分のみ）
    print("\n## 3. fuzzy matching（ratio）で新たに推定できた件数 / accuracy — 閾値別")
    for fy in fys:
        print(f"\nFY{fy}")
        print("| 正規化 | " + " | ".join(f"≥{t}" for t in thresholds) + " |")
        print("|---|" + "---:|" * len(thresholds))
        for m in phase3.NORMALIZERS:
            cells = []
            for t in thresholds:
                s = phase3.stage_summary(phase3.apply_threshold(preds[(m, fy)], t))
                report[f"fuzzy:{m}:{fy}:{t}"] = s
                cells.append(f"+{s['fuzzy']['n']} / {_pct(s['fuzzy']['accuracy'])}")
            print(f"| {m} | " + " | ".join(cells) + " |")

    # 正規化方式の選択: FY2022+FY2023 で ≥90 までの正解件数が最大のもの（FY2024 は見ない）
    def correct(m, fy, t):
        return phase3.stage_summary(phase3.apply_threshold(preds[(m, fy)], t))["total"]["correct"]
    best = max(phase3.NORMALIZERS, key=lambda m: sum(correct(m, fy, 90) for fy in (2022, 2023)))
    print(f"\n選択した正規化（FY2022+FY2023 の正解件数で選択）: {best}")
    wr = {fy: phase3.rolling_predictions(rows, fy, best, "WRatio") for fy in fys}
    print("\nscorer 比較（同じ正規化, fuzzy 追加分 件数 / accuracy）")
    print("| scorer | " + " | ".join(f"FY{fy} ≥90 / ≥80" for fy in fys) + " |")
    print("|---|" + "---:|" * len(fys))
    for name, pr in (("ratio", {fy: preds[(best, fy)] for fy in fys}), ("WRatio", wr)):
        cells = []
        for fy in fys:
            a, b = (phase3.stage_summary(phase3.apply_threshold(pr[fy], t))["fuzzy"] for t in (90, 80))
            cells.append(f"+{a['n']} / {_pct(a['accuracy'])} ・ +{b['n']} / {_pct(b['accuracy'])}")
        print(f"| {name} | " + " | ".join(cells) + " |")

    # 5/6. 段階別（選択した正規化, ratio）
    for t in (90, 80):
        print(f"\n## 5/6. 段階別の内訳（{best}, fuzzy ≥{t}）")
        print("| 年度 | 段階 | 件数（比率） | 主科目 accuracy | simple accuracy (件数) | complex accuracy (件数) |")
        print("|---|---|---:|---:|---:|---:|")
        for fy in fys:
            s = phase3.stage_summary(phase3.apply_threshold(preds[(best, fy)], t))
            for st, title in (("raw_exact", "exact raw"), ("norm_exact", "normalized exactで追加"), ("fuzzy", "fuzzyで追加"), ("none", "候補なし")):
                v = s[st]
                print(f"| FY{fy} | {title} | {v['n']} ({_pct(v['share'])}) | {_pct(v['accuracy'])} | "
                      + (f"{_pct(v['simple_acc'])} ({v['simple_n']}) | {_pct(v['complex_acc'])} ({v['complex_n']})" if st != "none" else f"- ({v['simple_n']}) | - ({v['complex_n']})") + " |")

    # 7. confidence 特徴（fuzzy は閾値なし＝全候補）
    pooled = [p for fy in fys for p in preds[(best, fy)]]
    fy24 = preds[(best, 2024)]
    def sb(p):
        if p["stage"] in ("raw_exact", "norm_exact"):
            return p["stage"]
        s = p["score"]
        return "fuzzy 95-99" if s >= 95 else "fuzzy 90-94" if s >= 90 else "fuzzy 85-89" if s >= 85 else "fuzzy 80-84" if s >= 80 else "fuzzy 70-79" if s >= 70 else "fuzzy <70"
    feats = [
        ("段階・類似度", sb, ["raw_exact", "norm_exact", "fuzzy 95-99", "fuzzy 90-94", "fuzzy 85-89", "fuzzy 80-84", "fuzzy 70-79", "fuzzy <70"]),
        ("過去出現回数", lambda p: "1" if p["hist_n"] == 1 else "2" if p["hist_n"] == 2 else "3-4" if p["hist_n"] < 5 else "5-9" if p["hist_n"] < 10 else "10+", ["1", "2", "3-4", "5-9", "10+"]),
        ("過去の主科目一致率", lambda p: "100%" if p["hist_agreement"] == 1 else "80-99%" if p["hist_agreement"] >= 0.8 else "<80%", ["100%", "80-99%", "<80%"]),
        ("top3候補の主科目一致（fuzzyのみ）", lambda p: ("一致" if p["top3_agree"] else "不一致") if p["stage"] == "fuzzy" else "exact", ["一致", "不一致", "exact"]),
        ("最終利用からの日数", lambda p: "≤31" if p["days_since_last"] <= 31 else "32-90" if p["days_since_last"] <= 90 else "91-365" if p["days_since_last"] <= 365 else ">365", ["≤31", "32-90", "91-365", ">365"]),
        ("simple/complex 履歴", lambda p: p["hist_complex"], ["simple", "mixed", "complex"]),
    ]
    print(f"\n## 7. confidence 特徴と主科目 accuracy（{best}, fuzzy は閾値なしの全候補）")
    print("| 特徴 | 値 | FY2022-24 件数 / accuracy | FY2024 件数 / accuracy |")
    print("|---|---|---:|---:|")
    for title, key, order in feats:
        a = {k: (n, acc) for k, n, acc in phase3.bucket_accuracy(pooled, key, order)}
        b = {k: (n, acc) for k, n, acc in phase3.bucket_accuracy(fy24, key, order)}
        for k in order:
            if k in a or k in b:
                print(f"| {title} | {k} | {a.get(k, (0, None))[0]} / {_pct(a.get(k, (0, None))[1])} | {b.get(k, (0, None))[0]} / {_pct(b.get(k, (0, None))[1])} |")

    combos = [
        ("exact・過去3回以上・一致率100%", lambda p: p["stage"] in ("raw_exact", "norm_exact") and p["hist_n"] >= 3 and p["hist_agreement"] == 1),
        ("exact・過去3回以上・一致率100%・365日以内", lambda p: p["stage"] in ("raw_exact", "norm_exact") and p["hist_n"] >= 3 and p["hist_agreement"] == 1 and p["days_since_last"] <= 365),
        ("exact・過去2回以上・一致率100%・simple履歴", lambda p: p["stage"] in ("raw_exact", "norm_exact") and p["hist_n"] >= 2 and p["hist_agreement"] == 1 and p["hist_complex"] == "simple"),
        ("exact・一致率≥80%", lambda p: p["stage"] in ("raw_exact", "norm_exact") and p["hist_agreement"] >= 0.8),
        ("fuzzy≥90・top3一致・一致率100%", lambda p: p["stage"] == "fuzzy" and p["score"] >= 90 and p["top3_agree"] and p["hist_agreement"] == 1),
        ("fuzzy≥85・top3一致", lambda p: p["stage"] == "fuzzy" and p["score"] >= 85 and p["top3_agree"]),
        ("fuzzy≥80・top3一致・一致率100%", lambda p: p["stage"] == "fuzzy" and p["score"] >= 80 and p["top3_agree"] and p["hist_agreement"] == 1),
    ]
    print("\n### 特徴の組み合わせ（主科目 accuracy）")
    print("| 条件 | " + " | ".join(f"FY{fy} 件数（全体比）/ accuracy" for fy in fys) + " |")
    print("|---|" + "---:|" * len(fys))
    for title, f in combos:
        cells = []
        for fy in fys:
            ps = [p for p in preds[(best, fy)] if p["stage"] != "none" and f(p)]
            ok = sum(1 for p in ps if p["correct"])
            cells.append(f"{len(ps)} ({_pct(len(ps) / len(preds[(best, fy)]))}) / {_pct(ok / len(ps) if ps else None)}")
        print(f"| {title} | " + " | ".join(cells) + " |")

    # 8. FY2024 比較
    print(f"\n## 8. FY2024 比較（主科目, {best}）")
    print("| 段階 | coverage | 主科目 accuracy | 正解件数 / 評価対象 |")
    print("|---|---:|---:|---:|")
    rawp = phase3.apply_threshold([p if p["stage"] != "norm_exact" else {**p, "stage": "none", "pred": None, "correct": None} for p in preds[(best, 2024)]], None)
    for title, ps in (("A. raw exact のみ", rawp), ("B. normalized exact まで", phase3.apply_threshold(preds[(best, 2024)], None)),
                      *((f"C. fuzzy ≥{t} まで", phase3.apply_threshold(preds[(best, 2024)], t)) for t in (90, 85, 80, 70))):
        s = phase3.stage_summary(ps)["total"]
        print(f"| {title} | {_pct(s['coverage'])} | {_pct(s['accuracy'])} | {s['correct']} / {len(ps)} |")

    for fy in fys:
        write_csv(out / f"predictions_fy{fy}.csv", preds[(best, fy)])
    save_json(out / "phase3_report.json", {"normalizer": best, **report})
    print(f"\n派生データ: {out}")


def _phase4_context(settings: Settings):
    office_code = settings.require_office_code()
    masters = _run_or_latest(settings, office_code, "masters", None)
    jrun = _run_or_latest(settings, office_code, "journals", None)
    trun = _run_or_latest(settings, office_code, "transactions", None)
    terms = load_json(masters / "term_settings.json").get("term_settings") or []
    connected = load_json(masters / "connected_accounts.json").get("connected_accounts") or []
    accounts = load_json(masters / "accounts.json").get("accounts") or []
    rows, _ = phase2.build_labels(_load_records(jrun, "journals"), _load_records(trun, "transactions"), terms, connected)
    rows = [r for r in rows if r["fiscal_year"] is not None and 2018 <= r["fiscal_year"] <= 2024]
    out = processed_dir(settings.data_dir, office_code) / "phase4"
    return rows, accounts, out


def _account_kind(bank_account_id: str | None, accounts_by_id: dict) -> str:
    a = accounts_by_id.get(bank_account_id) or {}
    if a.get("category") == "CASH_AND_DEPOSITS":
        return "銀行口座"
    if a.get("account_group") == "LIABILITY":
        return "クレジットカード等（負債科目で管理）"
    return "その他"


def cmd_llm_prepare(settings: Settings, args) -> None:
    """Phase 4 dry-run: 対象選定・payload 作成・コスト見積もり。LLM API は呼ばない。"""
    rows, accounts, out = _phase4_context(settings)
    target_fy = args.target_fy
    by_id = {a["id"]: a for a in accounts}
    names = {a["id"]: a.get("name") for a in accounts}
    catalog = phase4.Catalog.from_accounts(accounts, available_only=True)
    row_by_tx = {r["transaction_id"]: r for r in rows}

    preds = phase3.rolling_predictions(rows, target_fy, phase4.NORMALIZER, "ratio")
    enriched = []
    for p in preds:
        c = phase4.classify_target(p)
        r = row_by_tx[p["transaction_id"]]
        enriched.append({**p, **c, "tx_side": r["tx_side"], "tx_value": r["tx_value"], "bank_account_id": r["bank_account_id"]})
    high = [t for t in enriched if t["high_confidence"]]
    targets = [t for t in enriched if t["llm_target"]]
    other = [t for t in enriched if not t["high_confidence"] and not t["llm_target"]]
    if args.all and not args.exclude_e:  # 実運用パイプライン: 高confidence群以外はすべて LLM へ
        targets = [t for t in enriched if not t["high_confidence"]]
    cat_counts = Counter(c for t in targets for c in t["categories"])
    unreachable = sum(1 for t in targets if t["actual"] not in catalog.id_to_code)

    print(f"## 1. FY{target_fy} の対象選定（{len(enriched)}件）")
    print(f"- 高confidence群（LLM対象外）: {len(high)}件 — exact・過去3回以上・主科目一致率100%・最終利用365日以内")
    print(f"- LLM対象: {len(targets)}件（カテゴリ重複あり）: " + ", ".join(f"{k}={v}" for k, v in sorted(cat_counts.items())))
    print(f"- その他（E: exact・過去1〜2回で一致）: {len(other)}件" + ("（--all のため LLM対象に含める）" if args.all and not args.exclude_e else "（LLM対象外・人間確認候補）"))
    sig = Counter("+".join(c[0] for c in t["categories"]) for t in targets)
    print("- 条件の組み合わせ（重複を考慮）: " + ", ".join(f"{k}={v}" for k, v in sorted(sig.items())))
    print(f"- LLM対象のうち、正解の主科目が現在利用不可（選択肢にない）: {unreachable}件")

    sample = sorted(targets, key=lambda t: (t["tx_date"], t["transaction_id"])) if args.all else phase4.stratified_sample(targets, args.sample)
    ordered = phase4.build_candidate_index(rows)
    system = phase4.system_text(catalog, args.reason_category)
    schema = phase4.output_schema(catalog, args.reason_category)
    dry = out / args.name
    dry.mkdir(parents=True, exist_ok=True)
    # パイプライン評価用: 全明細のルール判定（正解を含むため送信しない）
    save_json(dry / "pipeline_rules_not_sent.json", [
        {"transaction_id": t["transaction_id"], "high_confidence": t["high_confidence"], "sent_to_llm": t in targets,
         "categories": t["categories"], "rule_stage": t["stage"], "rule_pred": t["pred"], "actual": t["actual"], "actual_complex": t["actual_complex"]}
        for t in enriched])
    items, answers = [], []
    for t in sample:
        cands = phase4.candidates_before(ordered, t["tx_date"], t["tx_content"], catalog.id_to_code, names)
        payload = phase4.build_payload({**t}, cands, _account_kind(t["bank_account_id"], by_id))
        items.append({"transaction_id": t["transaction_id"], "categories": t["categories"], "payload": payload, "user_text": phase4.user_text(payload)})
        answers.append({"transaction_id": t["transaction_id"], "actual_primary_account_id": t["actual"], "actual_code": catalog.id_to_code.get(t["actual"]),
                        "actual_complex": t["actual_complex"], "categories": t["categories"], "rule_stage": t["stage"], "rule_pred": t["pred"]})
    (dry / "system_prompt.txt").parent.mkdir(parents=True, exist_ok=True)
    (dry / "system_prompt.txt").write_text(system, encoding="utf-8")
    save_json(dry / "output_schema.json", schema)
    save_json(dry / "catalog.json", {"code_to_id": catalog.code_to_id})
    (dry / "payloads.jsonl").write_text("\n".join(json.dumps(i, ensure_ascii=False) for i in items) + "\n", encoding="utf-8")
    (dry / "answers_not_sent.jsonl").write_text("\n".join(json.dumps(a, ensure_ascii=False) for a in answers) + "\n", encoding="utf-8")

    # 送信前チェック（値は表示しない）
    import re as _re
    blob = "\n".join(i["user_text"] for i in items)
    leaks = {
        "7桁以上の数字": len(_re.findall(r"\d{7,}", blob)),
        "メール": len(phase4._EMAIL.findall(blob)),
        "電話番号": len(phase4._PHONE.findall(blob)),
        "正解の仕訳ID/明細ID": sum(1 for i, a in zip(items, answers) if a["transaction_id"] in i["user_text"]),
    }
    sc = Counter((("B" if "B_fuzzy候補のみ" in t["categories"] else "A" if "A_候補なし" in t["categories"] else "C" if "C_科目が割れている" in t["categories"] else "D" if "D_365日以上未使用" in t["categories"] else "E"), "complex" if t["actual_complex"] else "simple") for t in sample)
    ncand = Counter(len(i["payload"]["past_similar_transactions"]) for i in items)
    print(f"\n## 2. payload {len(sample)}件（{'全件' if args.all else '層別抽出, seed固定'}）")
    print("- 層（A=exactなし・候補なし（fuzzy<70含む）, B=exactなし・fuzzy候補(≥70)のみ, C=科目が割れている, D=長期未使用）×構造: " + ", ".join(f"{k[0]}/{k[1]}={v}" for k, v in sorted(sc.items())))
    print(f"- 過去候補の件数分布: {dict(sorted(ncand.items()))}（最大 {phase4.MAX_CANDIDATES}件）")
    print(f"- 選択可能な勘定科目: {len(catalog.rows)}（現在利用可能なもの, 短縮コード A001〜 → ローカルでMFのIDに変換）")
    print(f"- 送信前チェック（マスク後の残存数）: " + ", ".join(f"{k}={v}" for k, v in leaks.items()))

    sys_tok = phase4.estimate_tokens(system) + phase4.estimate_tokens(json.dumps(schema))
    user_toks = [phase4.estimate_tokens(i["user_text"]) for i in items]
    out_range = (150, 1200)  # JSON回答 + adaptive thinking（effort により変動）
    print(f"\n## 3. トークン・コスト見積もり（ローカル概算, API未使用）")
    print(f"- system（指示+科目一覧+スキーマ）: 約{sys_tok:,} tokens/件（プロンプトキャッシュ対象）")
    print(f"- user（明細+過去候補）: 平均 約{sum(user_toks) // len(user_toks):,} / 最大 約{max(user_toks):,} tokens/件")
    print(f"- output: 1件あたり {out_range[0]}〜{out_range[1]} tokens と仮定（構造化JSON + 思考トークン）")
    print("| モデル | 対象 | input tokens | output tokens | 推定コスト（キャッシュあり） |")
    print("|---|---:|---:|---:|---:|")
    full_user = [sum(user_toks) // len(user_toks)] * len(targets)
    for model in phase4.PRICES:
        for label, ut in ((f"dry-run {len(items)}件", user_toks), (f"FY{target_fy} LLM対象 全{len(targets)}件（参考）", full_user)):
            e = phase4.estimate_cost(model, sys_tok, ut, out_range)
            lo, hi = e["cost_usd_range_with_cache"]
            print(f"| {model} | {label} | 約{e['input_tokens']:,} | {e['output_tokens_range'][0]:,}〜{e['output_tokens_range'][1]:,} | ${lo:.2f}〜${hi:.2f} |")
    print(f"\n確認用ファイル（ローカルのみ, Git管理外）: {dry}")
    print("  system_prompt.txt / output_schema.json / payloads.jsonl（送信予定の内容）/ answers_not_sent.jsonl（正解, 送信しない）")


def _close_context(settings: Settings, fy: int):
    """決算処理用: 過去年度の履歴（仕訳紐付き）と、対象年度の明細・マスター（data/fy{fy}_close/raw）。"""
    office_code = settings.require_office_code()
    rows, accounts, out = _phase4_context(settings)
    history = [r for r in rows if r["fiscal_year"] < fy and r.get("tx_content") and r.get("primary_account_id")]
    close_dir = settings.data_dir / f"fy{fy}_close"
    masters = latest_run_dir(close_dir, office_code, "masters")
    trun = latest_run_dir(close_dir, office_code, "transactions")
    if masters is None or trun is None:
        raise ConfigError(f"{close_dir} に masters / transactions の取得データがありません。")
    connected = load_json(masters / "connected_accounts.json").get("connected_accounts") or []
    fy_accounts = load_json(masters / "accounts.json").get("accounts") or []
    if phase4.Catalog.from_accounts(fy_accounts).code_to_id != phase4.Catalog.from_accounts(accounts).code_to_id:
        raise ConfigError("対象年度の勘定科目マスターが FY2023/FY2024 検証時と異なります（catalog 不一致）。")
    return history, accounts, connected, _load_records(trun, "transactions"), out / f"fy{fy}_close", close_dir, trun


def cmd_close_prepare(settings: Settings, args) -> None:
    """決算処理: 対象抽出・楽天カード系統統合・Amazon 除外・層分け・Sonnet payload 作成（LLM API は呼ばない）。"""
    from . import close
    fy = args.fy
    history, accounts, connected, txs, dry, close_dir, trun = _close_context(settings, fy)
    by_id = {a["id"]: a for a in accounts}
    sub = {s["id"]: (a, s) for a in connected for s in a.get("connected_sub_accounts") or []}
    is_card = lambda sid: (by_id.get((sub.get(sid) or ({}, {}))[1].get("account_id")) or {}).get("account_group") == "LIABILITY"
    lineage = close.lineage_map(connected)

    amazon_linked, amazon_dup, targets = [], [], []
    for t in txs:
        a, _ = sub.get(t.get("connected_sub_account_id")) or ({}, {})
        r = {"transaction_id": t["id"], "tx_date": t["date"], "tx_content": (t.get("content") or "").strip(), "tx_value": t.get("value"),
             "tx_side": t.get("side"), "connected_sub_account_id": t.get("connected_sub_account_id"),
             "bank_account_id": (sub.get(t.get("connected_sub_account_id")) or ({}, {}))[1].get("account_id")}
        if a.get("name") == close.AMAZON_CONNECTED_ACCOUNT:
            amazon_linked.append(r)
        elif close.is_amazon_dup(r["tx_content"], is_card(r["connected_sub_account_id"])):
            amazon_dup.append(r)
        else:
            targets.append(r)
    items = close.classify(targets, history, lineage)
    sonnet = sorted((i for i in items if i["routing"] == "sonnet"), key=lambda i: (i["tx_date"], i["transaction_id"]))

    catalog = phase4.Catalog.from_accounts(accounts, available_only=True)
    names = {a["id"]: a.get("name") for a in accounts}
    ordered = phase4.build_candidate_index(history)
    system = phase4.system_text(catalog, True)
    schema = phase4.output_schema(catalog, True)
    payloads = []
    for t in sonnet:
        cands = phase4.candidates_before(ordered, t["tx_date"], t["tx_content"], catalog.id_to_code, names)
        payload = phase4.build_payload(t, cands, _account_kind(t["bank_account_id"], by_id))
        payloads.append({"transaction_id": t["transaction_id"], "categories": [t["layer"]], "payload": payload, "user_text": phase4.user_text(payload)})

    dry.mkdir(parents=True, exist_ok=True)
    (dry / "system_prompt.txt").write_text(system, encoding="utf-8")
    save_json(dry / "output_schema.json", schema)
    save_json(dry / "catalog.json", {"code_to_id": catalog.code_to_id})
    (dry / "payloads.jsonl").write_text("\n".join(json.dumps(i, ensure_ascii=False) for i in payloads) + "\n", encoding="utf-8")
    save_json(dry / "items.json", items)
    save_json(dry / "amazon_dup.json", amazon_dup)
    save_json(dry / "amazon_linked.json", amazon_linked)

    layers = Counter(i["layer"] for i in items)
    counts = {
        "fiscal_year": fy, "transactions_run": str(trun), "history_fiscal_years": sorted({r["fiscal_year"] for r in history}),
        "history_rows": len(history), "transactions": len(txs), "amazon_linked": len(amazon_linked), "amazon_dup_candidates": len(amazon_dup),
        "ai_targets": len(items), "layers": dict(sorted(layers.items())),
        "rule_candidates": sum(1 for i in items if i["routing"] == close.RULE_CANDIDATE),
        "high_confidence_unseen_account": sum(1 for i in items if i["high_confidence"] and i["routing"] == close.HUMAN),
        "sonnet_targets": len(sonnet), "e_layer_human": layers["E"],
        "unseen_account_after_lineage": sum(1 for i in items if i["account_seen"] is False),
        "conditions": {"account_lineages": close.ACCOUNT_LINEAGES,
                       "lineage_sub_accounts": {k: sorted(s for s, v in lineage.items() if v == f"lineage:{k}") for k in close.ACCOUNT_LINEAGES},
                       "amazon_connected_account": close.AMAZON_CONNECTED_ACCOUNT, "amazon_dup_keywords": close.AMAZON_DUP_KEYWORDS,
                       "amazon_dup_exclude": close.AMAZON_DUP_EXCLUDE, "amazon_dup_scope": "card (LIABILITY) accounts only",
                       "sonnet_layers": close.SONNET_LAYERS, "sonnet_min_conf": close.SONNET_MIN_CONF,
                       "rule": "high_confidence AND account_seen(lineage)", "plan": "Balanced (FY2023 preregistered)"},
    }
    save_json(dry / "counts.json", counts)
    save_json(close_dir / "counts.json", counts)
    print(json.dumps({k: v for k, v in counts.items() if k != "conditions"}, ensure_ascii=False, indent=1))
    # 送信前チェック（値は表示しない）
    import re as _re
    blob = "\n".join(i["user_text"] for i in payloads)
    print(f"送信前チェック: 7桁以上の数字={len(_re.findall(r'[0-9]{7,}', blob))} メール={len(phase4._EMAIL.findall(blob))} 電話={len(phase4._PHONE.findall(blob))} "
          f"明細ID={sum(1 for i in payloads if i['transaction_id'] in i['user_text'])}")
    print(f"保存先: {dry}")


def cmd_close_review(settings: Settings, args) -> None:
    """決算処理: Sonnet 結果と合わせて review_ai.csv / review_amazon_dup.csv を生成する（MF write なし）。"""
    from . import close
    fy = args.fy
    office_code = settings.require_office_code()
    _, accounts, out = _phase4_context(settings)
    dry = out / f"fy{fy}_close"
    close_dir = settings.data_dir / f"fy{fy}_close"
    masters = latest_run_dir(close_dir, office_code, "masters")
    connected = load_json(masters / "connected_accounts.json").get("connected_accounts") or []
    subnames = {s["id"]: f"{a['name']} / {s['name']}" for a in connected for s in a.get("connected_sub_accounts") or []}
    names = {a["id"]: a.get("name") for a in accounts}
    items = load_json(dry / "items.json")
    path = dry / args.results
    results = {r["transaction_id"]: r for r in (json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip())} if path.exists() else {}
    missing = [i for i in items if i["routing"] == "sonnet" and i["transaction_id"] not in results]
    if missing:
        raise ConfigError(f"Sonnet 結果が {len(missing)}件 不足しています（llm-run --resume で完了させてください）。")
    rows = close.review_rows(items, results, names, subnames)
    review = close_dir / "review"
    write_csv(review / "review_ai.csv", rows, close.REVIEW_AI_COLUMNS)

    linked = load_json(dry / "amazon_linked.json")
    dist = lambda a, b: abs((date.fromisoformat(a) - date.fromisoformat(b)).days)
    dup = []
    for r in sorted(load_json(dry / "amazon_dup.json"), key=lambda r: (r["tx_date"], r["transaction_id"])):
        m = sum(1 for x in linked if x["tx_value"] == r["tx_value"] and dist(x["tx_date"], r["tx_date"]) <= 7)
        dup.append({"transaction_id": r["transaction_id"], "transaction_date": r["tx_date"], "transaction_content": r["tx_content"],
                    "amount": r["tx_value"], "side": r["tx_side"], "connected_account": subnames.get(r["connected_sub_account_id"], ""),
                    "amazon_linked_same_amount_within_7d": m})
    write_csv(review / "review_amazon_dup.csv", dup, close.REVIEW_AMAZON_COLUMNS)

    son = [r for r in rows if r["inference_source"] == "sonnet"]
    usage = [r["usage"] for r in results.values()]
    summary = {
        "ai_targets": len(rows), "routing": dict(Counter(r["routing"] for r in rows)),
        "rule_candidates": sum(1 for r in rows if r["routing"] == close.RULE_CANDIDATE),
        "sonnet_processed": len(son),
        "sonnet_conf_bands": dict(Counter(close.conf_band(r["confidence"] if r["confidence"] != "" else None) for r in son)),
        "sonnet_auto_candidates": sum(1 for r in rows if r["routing"] == close.SONNET_AUTO),
        "sonnet_to_human": sum(1 for r in son if r["routing"] == close.HUMAN),
        "auto_candidates": sum(1 for r in rows if r["routing"] in (close.RULE_CANDIDATE, close.SONNET_AUTO)),
        "human_review": sum(1 for r in rows if r["routing"] == close.HUMAN),
        "review_groups": len({r["review_group"] for r in rows}),
        "review_groups_by_routing": dict(Counter(r["routing"] for r in {r["review_group"]: r for r in rows}.values())),
        "amazon_dup_candidates": len(dup),
        "usage": {k: sum(u[k] for u in usage) for k in ("input", "cache_write", "cache_read", "output")} if usage else None,
        "estimated_cost_usd": round(sum(phase4.usage_cost("claude-sonnet-5", r["usage"]) for r in results.values()), 2),
    }
    save_json(review / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"保存先: {review}")


def cmd_close_groups(settings: Settings, args) -> None:
    """決算処理: 1行=1レビューグループの review_groups.csv と、明細への展開用 review_group_members.csv（review_ai.csv は変更しない）。"""
    from . import close
    fy = args.fy
    history, accounts, connected, _, dry, close_dir, _ = _close_context(settings, fy)
    subnames = {s["id"]: f"{a['name']} / {s['name']}" for a in connected for s in a.get("connected_sub_accounts") or []}
    names = {a["id"]: a.get("name") for a in accounts}
    items = load_json(dry / "items.json")
    path = dry / args.results
    results = {r["transaction_id"]: r for r in (json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip())}
    if any(i["routing"] == "sonnet" and i["transaction_id"] not in results for i in items):
        raise ConfigError("Sonnet 結果が不足しています。")
    rows = close.review_rows(items, results, names, subnames)
    catalog = phase4.Catalog.from_accounts(accounts, available_only=True)
    ordered = phase4.build_candidate_index(history)
    past = {i["transaction_id"]: close.past_summary(phase4.candidates_before(ordered, i["tx_date"], i["tx_content"], catalog.id_to_code, names))
            for i in items}
    groups, members = close.review_groups(rows, past)
    review = close_dir / "review"
    write_csv(review / "review_groups.csv", groups, close.REVIEW_GROUP_COLUMNS)
    write_csv(review / "review_group_members.csv", members, close.REVIEW_MEMBER_COLUMNS)
    by = lambda rt: [g for g in groups if g["routing"] == rt]
    summary = {rt: {"groups": len(by(rt)), "rows": sum(g["group_size"] for g in by(rt)),
                    "single_row_groups": sum(1 for g in by(rt) if g["group_size"] == 1)} for rt in close.GROUP_ORDER}
    summary["total"] = {"groups": len(groups), "rows": len(members)}
    save_json(review / "review_groups_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"保存先: {review}")


def cmd_llm_run(settings: Settings, args) -> None:
    rows, accounts, out = _phase4_context(settings)
    dry = out / args.name
    items = [json.loads(l) for l in (dry / "payloads.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    catalog = phase4.Catalog.from_accounts(accounts, available_only=True)
    if load_json(dry / "catalog.json")["code_to_id"] != catalog.code_to_id:
        raise ConfigError("勘定科目カタログが dry-run 時と異なります。llm-prepare をやり直してください。")
    system = (dry / "system_prompt.txt").read_text(encoding="utf-8")
    schema = load_json(dry / "output_schema.json")

    path = dry / f"results_{args.model}_{args.effort}.jsonl"
    done_ids = set()
    if args.resume and path.exists():
        done_ids = {json.loads(l)["transaction_id"] for l in path.read_text(encoding="utf-8").splitlines() if l.strip()}
        items = [i for i in items if i["transaction_id"] not in done_ids]
        print(f"再開: 完了済み {len(done_ids)}件をスキップ、残り {len(items)}件")
    if not items:
        print("実行対象がありません。")
        return

    # 1. 実際の入力トークン数
    est = [phase4.estimate_tokens(system) + phase4.estimate_tokens(json.dumps(schema)) + phase4.estimate_tokens(i["user_text"]) for i in items]
    try:
        counts = phase4.count_tokens(items, system, schema, args.model, args.effort, approved=args.approve)
    except phase4.LLMRunError as e:
        print(f"[停止] {e}（実行はしていません）", file=sys.stderr)
        raise SystemExit(5)
    save_json(dry / "token_counts.json", {"model": args.model, "counts": counts, "estimates": est})
    print(f"count_tokens: 合計 {sum(counts):,} tokens（1件 平均 {sum(counts) // len(counts):,} / 最小 {min(counts):,} / 最大 {max(counts):,}）、ローカル概算 {sum(est):,}")
    if sum(counts) > 2 * sum(est):
        raise ConfigError("実際のトークン数が見積もりの2倍を超えたため、実行せずに停止しました。")
    # キャッシュ対象（system + スキーマ）は全件共通。40件実行時の実績では cache_write = 共通部分
    shared = phase4.shared_tokens(system, schema, args.model, args.effort, approved=args.approve)
    n = len(counts)
    uncached = sum(counts) - shared * n
    pin, pout, pcache = phase4.PRICES[args.model]
    exp_out = args.expected_output * n
    cost_cached = (uncached * pin + shared * pin * 1.25 + shared * (n - 1) * pcache + exp_out * pout) / 1e6
    cost_nocache = (sum(counts) * pin + exp_out * pout) / 1e6
    print(f"キャッシュ対象 {shared:,} tokens/件 → cache write {shared:,} / cache read {shared * (n - 1):,} / uncached {uncached:,}")
    print(f"想定 output {args.expected_output} tokens/件 × {n} = {exp_out:,}")
    print(f"推定コスト: キャッシュあり ${cost_cached:.2f} / キャッシュなし ${cost_nocache:.2f}（上限 ${args.max_cost:.2f}）")
    if cost_cached > args.max_cost:
        raise ConfigError(f"推定コストが上限 ${args.max_cost:.2f} を超えるため、実行せずに停止しました。")
    if args.count_only:
        return

    # 2. 実行（1件ずつ保存。異常時は停止）
    if not args.resume:
        path.write_text("", encoding="utf-8")
    spent = 0.0
    if args.resume and path.exists():
        spent = sum(phase4.usage_cost(args.model, json.loads(l)["usage"]) for l in path.read_text(encoding="utf-8").splitlines() if l.strip())
    def append(rec):
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    try:
        results = phase4.run_llm(items, system, schema, catalog, args.model, args.effort, approved=args.approve, on_result=append,
                                 max_cost=args.max_cost, spent=spent)
    except phase4.LLMRunError as e:
        done = sum(1 for l in path.read_text(encoding="utf-8").splitlines() if l.strip())
        print(f"[停止] {e}（累計 {done}件完了で停止。fallback はしていません。--resume で再開可能）", file=sys.stderr)
        raise SystemExit(5)
    results = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    u = [r["usage"] for r in results]
    print(f"{len(results)}件 完了。usage 合計: input {sum(x['input'] for x in u):,} / cache_write {sum(x['cache_write'] for x in u):,} / "
          f"cache_read {sum(x['cache_read'] for x in u):,} / output {sum(x['output'] for x in u):,} tokens")
    price = phase4.PRICES.get(args.model)
    if price:
        pin, pout, pcache = price
        cost = (sum(x["input"] for x in u) * pin + sum(x["cache_write"] for x in u) * pin * 1.25 + sum(x["cache_read"] for x in u) * pcache + sum(x["output"] for x in u) * pout) / 1e6
        print(f"推定コスト（usage から計算）: ${cost:.2f}")
    print(f"保存先: {path}")


def cmd_llm_tokens(settings: Settings, args) -> None:
    """count_tokens の差分計測で入力トークンの内訳を出し、モデル別コストを試算する（推論は行わない）。"""
    rows, accounts, out = _phase4_context(settings)
    dry = out / "dry_run"
    items = [json.loads(l) for l in (dry / "payloads.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    catalog = phase4.Catalog.from_accounts(accounts, available_only=True)
    schema = load_json(dry / "output_schema.json")
    try:
        bd = phase4.token_breakdown(items, catalog.text(), schema, args.models, approved=args.approve)
    except phase4.LLMRunError as e:
        print(f"[停止] {e}", file=sys.stderr)
        raise SystemExit(5)
    save_json(dry / "token_breakdown.json", bd)
    n = len(items)
    for model, b in bd.items():
        sh, pi = b["shared"], b["per_item"]
        shared_total = sum(sh.values())
        print(f"\n## {model}（{n}件）")
        print(f"- 40件合計 input: {b['total']:,} / 1件平均: {b['per_item_avg']:,.0f}")
        print(f"- 共通部分: 合計 {shared_total:,}/件 = system指示 {sh['system_instructions']:,} + 勘定科目一覧 {sh['account_catalog']:,} + 出力スキーマ {sh['output_schema']:,} + thinking/effort設定 {sh['thinking_effort_config']:,} + メッセージ枠 {sh['message_overhead']:,}")
        print(f"- 明細ごと: 平均 {sum(pi.values()):,.0f}/件 = 明細情報 {pi['transaction']:,.0f} + 過去候補 {pi['past_candidates']:,.0f} + 定型文・JSON枠 {pi['wrapper_text']:,.0f}")
        print(f"- 40件合計の内訳: 共通部分×{n} = {shared_total * n:,}（{shared_total * n / b['total']:.0%}） / 明細ごと = {b['total'] - shared_total * n:,}（{(b['total'] - shared_total * n) / b['total']:.0%}）")
        print("- 出力JSONのtoken数（見本）: " + ", ".join(f"{k} ≈ {v}" for k, v in b["output_json_samples"].items()))
    print(f"\n保存先: {dry / 'token_breakdown.json'}")


def cmd_llm_compare(settings: Settings, args) -> None:
    """2モデルの結果を同一サンプルで比較する（件数・率のみ表示）。"""
    office_code = settings.require_office_code()
    dry = processed_dir(settings.data_dir, office_code) / "phase4" / "dry_run"
    def load(name):
        return {r["transaction_id"]: (r.get("output") or {}) for r in (json.loads(l) for l in (dry / name).read_text(encoding="utf-8").splitlines() if l.strip())}
    a_res, b_res = load(args.a), load(args.b)
    ans = [json.loads(l) for l in (dry / "answers_not_sent.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    A, B = args.a_label, args.b_label
    rows = []
    for x in ans:
        a, b = a_res[x["transaction_id"]], b_res[x["transaction_id"]]
        act = x["actual_primary_account_id"]
        rows.append({"a": a.get("primary_account_id"), "b": b.get("primary_account_id"), "ac": a.get("confidence") or 0.0, "bc": b.get("confidence") or 0.0,
                     "ar": a.get("needs_review"), "br": b.get("needs_review"), "a_ok": a.get("primary_account_id") == act, "b_ok": b.get("primary_account_id") == act})
    n = len(rows)
    def cov(sel, ok):
        rs = [r for r in rows if sel(r)]
        k = sum(1 for r in rs if ok(r))
        return f"{len(rs)}/{n} ({_pct(len(rs) / n)}) / {_pct(k / len(rs) if rs else None)}"
    g = Counter((r["a_ok"], r["b_ok"]) for r in rows)
    print(f"## {A} vs {B}（同一 {n}件）")
    print(f"- 両方正解 {g[(True, True)]} / {A}だけ正解 {g[(True, False)]} / {B}だけ正解 {g[(False, True)]} / 両方不正解 {g[(False, False)]}")
    same = [r for r in rows if r["a"] and r["a"] == r["b"]]
    print(f"- 同じ科目を選択: {len(same)}件, accuracy {_pct(sum(r['a_ok'] for r in same) / len(same) if same else None)} / 異なる科目: {n - len(same)}件"
          f"（うち {A}正解 {sum(r['a_ok'] for r in rows if r not in same)} / {B}正解 {sum(r['b_ok'] for r in rows if r not in same)}）")
    print(f"- 両方 confidence>=0.70 かつ同じ科目: coverage / accuracy = {cov(lambda r: r['ac'] >= 0.7 and r['bc'] >= 0.7 and r['a'] == r['b'] and r['a'], lambda r: r['a_ok'])}")
    print("\n## confidence calibration（coverage / accuracy）")
    print(f"| 条件 | {A} | {B} |\n|---|---:|---:|")
    for t in (0.7, 0.8, 0.9):
        print(f"| confidence >= {t:.2f} | {cov(lambda r: r['ac'] >= t, lambda r: r['a_ok'])} | {cov(lambda r: r['bc'] >= t, lambda r: r['b_ok'])} |")
    print(f"| needs_review = false | {cov(lambda r: r['ar'] is False, lambda r: r['a_ok'])} | {cov(lambda r: r['br'] is False, lambda r: r['b_ok'])} |")
    print("\n## ensemble 候補（coverage / accuracy）")
    print(f"- A: {A}>=0.70 AND {B}>=0.70 AND 同じ科目: {cov(lambda r: r['ac'] >= 0.7 and r['bc'] >= 0.7 and r['a'] == r['b'] and r['a'], lambda r: r['a_ok'])}")
    print(f"- B: {B}>=0.70 のみ: {cov(lambda r: r['bc'] >= 0.7, lambda r: r['b_ok'])}")
    print(f"- C: {A}>=0.70 のみ: {cov(lambda r: r['ac'] >= 0.7, lambda r: r['a_ok'])}")
    save_json(dry / f"compare_{args.a_label}_vs_{args.b_label}.json", {"agreement": dict((f"{k[0]}_{k[1]}", v) for k, v in g.items()), "n": n})


def cmd_llm_pipeline_eval(settings: Settings, args) -> None:
    """FY2024 全件の実運用パイプライン評価（ルール → LLM → 人間確認）。件数・率のみ表示。"""
    office_code = settings.require_office_code()
    d = processed_dir(settings.data_dir, office_code) / "phase4" / args.name
    rules = load_json(d / "pipeline_rules_not_sent.json")
    res = {r["transaction_id"]: r for r in (json.loads(l) for l in (d / args.results).read_text(encoding="utf-8").splitlines() if l.strip())}
    N = len(rules)
    rows = []
    for r in rules:
        o = (res.get(r["transaction_id"]) or {}).get("output") or {}
        rows.append({**r, "llm": o.get("primary_account_id"), "conf": o.get("confidence"), "nr": o.get("needs_review"),
                     "insuff": o.get("insufficient_information"), "rcat": o.get("reason_category"), "has_llm": bool(o)})
    for r in rows:
        r["rule_ok"] = r["rule_pred"] is not None and r["rule_pred"] == r["actual"]
        r["llm_ok"] = r["llm"] is not None and r["llm"] == r["actual"]
        # ルールのみの予測: A 層は有効な候補なし（fuzzy<70 or 候補なし）
        r["rule_only_ok"] = r["rule_ok"] and "A_候補なし" not in r["categories"]
    high = [r for r in rows if r["high_confidence"]]
    llm = [r for r in rows if r["sent_to_llm"]]
    missing = [r for r in llm if not r["has_llm"]]
    pct = lambda a, b: _pct(a / b if b else None)
    acc = lambda rs, k="llm_ok": f"{sum(r[k] for r in rs)}/{len(rs)} ({pct(sum(r[k] for r in rs), len(rs))})"
    print(f"FY2024: {N}件 = 高confidenceルール {len(high)} + LLM対象 {len(llm)}（うち結果なし {len(missing)}）\n")

    print("## 5. Sonnet 単体（LLM対象）")
    print(f"- accuracy: {acc(llm)} / 参考: 同じ明細でルールのみ {acc(llm, 'rule_only_ok')}")
    print("| 条件（その条件を持つ明細, 重複あり） | 件数 | Sonnet accuracy | ルールのみ accuracy |\n|---|---:|---:|---:|")
    for c in ("A_候補なし", "B_fuzzy候補のみ", "C_科目が割れている", "D_365日以上未使用", "E_exact過去1〜2回"):
        rs = [r for r in llm if c in r["categories"]]
        print(f"| {c} | {len(rs)} | {acc(rs)} | {acc(rs, 'rule_only_ok')} |")
    print("| 条件の組み合わせ（重複なし） | 件数 | Sonnet accuracy | ルールのみ accuracy |\n|---|---:|---:|---:|")
    for sig in sorted({"+".join(c[0] for c in r["categories"]) for r in llm}):
        rs = [r for r in llm if "+".join(c[0] for c in r["categories"]) == sig]
        print(f"| {sig} | {len(rs)} | {acc(rs)} | {acc(rs, 'rule_only_ok')} |")
    for label, rs in (("simple", [r for r in llm if not r["actual_complex"]]), ("complex", [r for r in llm if r["actual_complex"]])):
        print(f"- {label}: {acc(rs)}")

    print("\n## 6. confidence calibration（LLM対象 {} 件）".format(len(llm)))
    print("| 条件 | 件数 | coverage | accuracy | 誤り |\n|---|---:|---:|---:|---:|")
    def cal(label, sel):
        rs = [r for r in llm if r["has_llm"] and sel(r)]
        ok = sum(r["llm_ok"] for r in rs)
        print(f"| {label} | {len(rs)} | {pct(len(rs), len(llm))} | {pct(ok, len(rs))} | {len(rs) - ok} |")
    for t in (0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95):
        cal(f"confidence >= {t:.2f}", lambda r, t=t: r["conf"] >= t)
    cal("needs_review = false", lambda r: r["nr"] is False)
    cal("confidence >= 0.70 AND needs_review = false", lambda r: r["conf"] >= 0.7 and r["nr"] is False)

    def simulate(t, require_nr_false, exclude_insuff):
        auto_llm = [r for r in llm if r["has_llm"] and r["llm"] and r["conf"] >= t and (not require_nr_false or r["nr"] is False) and (not exclude_insuff or not r["insuff"])]
        auto = len(high) + len(auto_llm)
        ok = sum(r["rule_ok"] for r in high) + sum(r["llm_ok"] for r in auto_llm)
        return {"rule": len(high), "rule_ok": sum(r["rule_ok"] for r in high), "llm": len(auto_llm), "llm_ok": sum(r["llm_ok"] for r in auto_llm),
                "auto": auto, "ok": ok, "err": auto - ok, "human": N - auto}
    s0 = simulate(0.7, True, False)
    print("\n## 7. FY2024 全体の実運用シミュレーション（仮条件: Sonnet confidence >= 0.70 AND needs_review = false）")
    print(f"- A. 高confidenceルール: {s0['rule']}件 / accuracy {pct(s0['rule_ok'], s0['rule'])}（誤り {s0['rule'] - s0['rule_ok']}）")
    print(f"- B. Sonnet 自動: {s0['llm']}件 / accuracy {pct(s0['llm_ok'], s0['llm'])}（誤り {s0['llm'] - s0['llm_ok']}）")
    print(f"- C. 人間確認: {s0['human']}件（{pct(s0['human'], N)}）")
    print(f"- 自動判定 合計 {s0['auto']}件 / coverage {pct(s0['auto'], N)} / accuracy {pct(s0['ok'], s0['auto'])} / 誤り {s0['err']}件")

    print("\n## 8. 閾値感度（FY2024 全{}件）".format(N))
    print("| Sonnet 自動条件 | automation coverage | automated accuracy | 誤り | 人間確認 |\n|---|---:|---:|---:|---:|")
    sims = {}
    for nr, ex, label in ((False, False, "confidence のみ"), (True, False, "+ needs_review=false"), (True, True, "+ needs_review=false + insufficient除外")):
        for t in (0.6, 0.7, 0.8, 0.9):
            x = simulate(t, nr, ex)
            sims[f"{label}:{t}"] = x
            print(f"| conf >= {t:.2f} {label} | {x['auto']} ({pct(x['auto'], N)}) | {pct(x['ok'], x['auto'])} | {x['err']} | {x['human']} |")
    x = {"auto": len(high), "ok": sum(r["rule_ok"] for r in high)}
    print(f"| 参考: ルールのみ（LLMなし） | {x['auto']} ({pct(x['auto'], N)}) | {pct(x['ok'], x['auto'])} | {x['auto'] - x['ok']} | {N - x['auto']} |")

    print("\n## 10. insufficient_information")
    ins = [r for r in llm if r["insuff"]]
    print(f"- true: {len(ins)}件, accuracy {pct(sum(r['llm_ok'] for r in ins), len(ins))}, needs_review=true {sum(1 for r in ins if r['nr'])}件")
    band = lambda c: ">=0.90" if c >= 0.9 else "0.80-0.89" if c >= 0.8 else "0.70-0.79" if c >= 0.7 else "0.50-0.69" if c >= 0.5 else "<0.50"
    print("- confidence 分布: " + ", ".join(f"{k}={v}" for k, v in sorted(Counter(band(r["conf"]) for r in ins).items())))
    print(f"- うち confidence>=0.70 かつ needs_review=false（除外しないと自動処理される件数）: {sum(1 for r in ins if r['conf'] >= 0.7 and r['nr'] is False)}")

    print("\n## reason_category")
    for k, v in Counter(r["rcat"] for r in llm if r["has_llm"]).most_common():
        rs = [r for r in llm if r["rcat"] == k]
        print(f"- {k}: {v}件, accuracy {pct(sum(r['llm_ok'] for r in rs), len(rs))}, 平均confidence {sum(r['conf'] for r in rs) / len(rs):.2f}")

    base = d.parent / "dry_run" / args.results
    if base.exists():
        b = {r["transaction_id"]: (r.get("output") or {}).get("primary_account_id") for r in (json.loads(l) for l in base.read_text(encoding="utf-8").splitlines() if l.strip())}
        common = [r for r in llm if r["transaction_id"] in b]
        same = sum(1 for r in common if r["llm"] == b[r["transaction_id"]])
        print(f"\n参考: 40件ベースラインと重なる {len(common)}件で予測が一致 {same}件（今回は reason_category 追加・過去候補は同一）")
    save_json(d / f"pipeline_eval_{args.results.replace('.jsonl', '')}.json", {"N": N, "high": len(high), "llm": len(llm), "simulations": sims})


def cmd_route_eval(settings: Settings, args) -> None:
    """候補ルーティングを FY2024 全件で評価（LLM API は呼ばない。件数・率のみ）。"""
    rows, accounts, out = _phase4_context(settings)
    d = out / args.name
    rules = load_json(d / "pipeline_rules_not_sent.json")
    res = {r["transaction_id"]: r["output"] for r in (json.loads(l) for l in (d / args.results).read_text(encoding="utf-8").splitlines() if l.strip())}
    items = routing.build_items(rows, rules, res, {a["id"]: a for a in accounts})
    print(f"| 案 | automation coverage | automated accuracy | 誤り | 人間確認 | 内訳 |\n|---|---:|---:|---:|---:|---|")
    for name, t in routing.PLANS.items():
        r = routing.route(items, routing.plan(t))
        print(f"| {name}（Sonnet conf>={t}） | {r['auto']} ({r['coverage']:.1%}) | {_pct(r['accuracy'])} | {r['errors']} | {r['human']} | ルール {r['by'].get('rule', 0)} / Sonnet {r['by'].get('llm', 0)} |")


def cmd_llm_eval(settings: Settings, args) -> None:
    """LLM 結果を正解（送信していないファイル）と照合する。件数・率のみ表示し、reason の本文は表示しない。"""
    office_code = settings.require_office_code()
    dry = processed_dir(settings.data_dir, office_code) / "phase4" / "dry_run"
    res = {r["transaction_id"]: r for r in (json.loads(l) for l in (dry / args.results).read_text(encoding="utf-8").splitlines() if l.strip())}
    ans = [json.loads(l) for l in (dry / "answers_not_sent.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    layer = lambda c: next((k[0] for k in ("B_fuzzy候補のみ", "A_候補なし", "C_科目が割れている", "D_365日以上未使用") if k in c), "?")
    rows = []
    for a in ans:
        o = (res.get(a["transaction_id"]) or {}).get("output") or {}
        lay = layer(a["categories"])
        rule_pred = None if lay == "A" else a["rule_pred"]  # A層はルールの有効な候補なし（fuzzy<70 or 候補なし）
        rows.append({"layer": lay, "complex": a["actual_complex"], "actual": a["actual_primary_account_id"],
                     "llm": o.get("primary_account_id"), "rule": rule_pred, "conf": o.get("confidence"),
                     "review": o.get("needs_review"), "insuff": o.get("insufficient_information"),
                     "reasons": phase4.reason_categories(o.get("reason")), "answered": bool(o)})
    for r in rows:
        r["llm_ok"] = r["llm"] is not None and r["llm"] == r["actual"]
        r["rule_ok"] = r["rule"] is not None and r["rule"] == r["actual"]

    def line(rs):
        n = len(rs)
        ok = sum(r["llm_ok"] for r in rs)
        confs = [r["conf"] for r in rs if r["conf"] is not None]
        rok = sum(r["rule_ok"] for r in rs)
        return (f"| {n} | {ok} ({_pct(ok / n if n else None)}) | {rok} ({_pct(rok / n if n else None)}) | "
                f"{(sum(confs) / len(confs)) if confs else 0:.2f} | {sum(1 for r in rs if r['review'])} | {sum(1 for r in rs if r['insuff'])} |")
    print(f"回答あり {sum(r['answered'] for r in rows)}/{len(rows)}件\n")
    print("## 層別（A=候補なし, B=fuzzy候補のみ, C=科目が割れている, D=365日以上未使用）")
    print("| 層 | 件数 | LLM 主科目 accuracy | ルールのみ accuracy | confidence 平均 | needs_review | insufficient_information |")
    print("|---|---:|---:|---:|---:|---:|---:|")
    for lay in "ABCD":
        print(f"| {lay} " + line([r for r in rows if r["layer"] == lay]))
    print("| 全体 " + line(rows))
    print("| (simple) " + line([r for r in rows if not r["complex"]]))
    print("| (complex) " + line([r for r in rows if r["complex"]]))

    band = lambda c: "0.90以上" if c >= 0.9 else "0.80〜0.89" if c >= 0.8 else "0.70〜0.79" if c >= 0.7 else "0.70未満"
    bands = ["0.90以上", "0.80〜0.89", "0.70〜0.79", "0.70未満"]
    print("\n## confidence 帯ごとの accuracy（LLM 自己評価と実際の正解率）")
    print("| confidence | 全体 件数 / accuracy | " + " | ".join(f"{l}層" for l in "ABCD") + " |")
    print("|---|---:|" + "---:|" * 4)
    for b in bands:
        rs = [r for r in rows if r["conf"] is not None and band(r["conf"]) == b]
        cell = lambda xs: f"{sum(x['llm_ok'] for x in xs)}/{len(xs)}" if xs else "-"
        print(f"| {b} | {len(rs)} / {_pct(sum(r['llm_ok'] for r in rs) / len(rs) if rs else None)} | " + " | ".join(cell([r for r in rs if r["layer"] == l]) for l in "ABCD") + " |")
    for key, title in (("review", "needs_review"), ("insuff", "insufficient_information")):
        for v in (False, True):
            rs = [r for r in rows if bool(r[key]) == v]
            print(f"- {title}={str(v).lower()}: {len(rs)}件, accuracy {_pct(sum(r['llm_ok'] for r in rs) / len(rs) if rs else None)}")

    print("\n## ルール vs LLM")
    grid = Counter((r["rule_ok"], r["llm_ok"]) for r in rows)
    print(f"- LLMのみ正解: {grid[(False, True)]} / ルールのみ正解: {grid[(True, False)]} / 両方正解: {grid[(True, True)]} / 両方不正解: {grid[(False, False)]}")
    for lay in "ABCD":
        g = Counter((r["rule_ok"], r["llm_ok"]) for r in rows if r["layer"] == lay)
        print(f"  - {lay}層: LLMのみ {g[(False, True)]} / ルールのみ {g[(True, False)]} / 両方 {g[(True, True)]} / 両方不正解 {g[(False, False)]}")

    print("\n## reason のカテゴリ（キーワードによる自動分類, 複数該当あり）")
    rc = Counter(c for r in rows for c in r["reasons"])
    print("- " + ", ".join(f"{k}={v}" for k, v in rc.most_common()))
    for c in [k for k, _ in rc.most_common()]:
        rs = [r for r in rows if c in r["reasons"]]
        print(f"  - {c}: {len(rs)}件, accuracy {_pct(sum(r['llm_ok'] for r in rs) / len(rs))}")
    save_json(dry / f"eval_{args.results.replace('.jsonl', '')}.json",
              {"rows": [{k: v for k, v in r.items() if k not in ("actual", "llm", "rule")} for r in rows]})


# ---- entry ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mf_accounting", description="MFクラウド会計 読み取り専用データ取得")
    p.add_argument("--env-file", default=None, help=".env のパス（既定: カレントの .env）")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("offices", help="Step 1: 認証と accessible_offices の取得").set_defaults(func=cmd_offices)
    sub.add_parser("masters", help="Step 2: 事業者情報・マスターの取得").set_defaults(func=cmd_masters)

    sub.add_parser("counts", help="各会計期間の仕訳件数のみ確認").set_defaults(func=cmd_counts)

    for name, func in (("journals", cmd_journals), ("transactions", cmd_transactions)):
        sp = sub.add_parser(name, help=f"{name} の取得")
        g = sp.add_mutually_exclusive_group()
        g.add_argument("--sample", type=int, default=10, help="少量取得の件数（既定 10）")
        g.add_argument("--all", action="store_true", help="全会計期間を取得（Step 6）")
        sp.add_argument("--start", help="少量取得の開始日 YYYY-MM-DD")
        sp.add_argument("--end", help="少量取得の終了日 YYYY-MM-DD")
        if name == "journals":
            sp.add_argument("--exclude-opening", action="store_true", help="少量取得で開始仕訳を除外")
        sp.set_defaults(func=func)

    sp = sub.add_parser("linkcheck", help="明細 → transaction_id → 仕訳 の紐付け確認（最新の明細取得分を使用）")
    sp.add_argument("--run", help="明細の取得ディレクトリ（既定: 最新）")
    sp.set_defaults(func=cmd_linkcheck)

    sp = sub.add_parser("inspect", help="取得済みJSONの構造要約（値は表示しない）")
    sp.add_argument("kind", choices=["journals", "transactions"])
    sp.add_argument("--run", help="対象の取得ディレクトリ（既定: 最新）")
    sp.set_defaults(func=cmd_inspect)

    sp = sub.add_parser("report", help="年度別の品質集計（値は表示しない）")
    sp.add_argument("--from-fy", type=int, default=2018)
    sp.add_argument("--to-fy", type=int, default=2024)
    sp.add_argument("--journals-run")
    sp.add_argument("--transactions-run")
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("analyze", help="Phase 2準備: 過去データのみでの相手科目推定の評価（値は表示しない）")
    sp.add_argument("--from-fy", type=int, default=2018)
    sp.add_argument("--to-fy", type=int, default=2024)
    sp.add_argument("--eval-fy", type=int, nargs="+", default=[2022, 2023, 2024])
    sp.add_argument("--journals-run")
    sp.add_argument("--transactions-run")
    sp.set_defaults(func=cmd_analyze)

    sp = sub.add_parser("analyze25", help="Phase 2.5: 直近重視・年度重み・方針変更検知の比較（値は表示しない）")
    sp.add_argument("--target-fy", type=int, default=2024)
    sp.add_argument("--eval-fy", type=int, nargs="+", default=[2024, 2023, 2022])
    sp.set_defaults(func=cmd_analyze25)

    sp = sub.add_parser("analyze3", help="Phase 3: 正規化 + fuzzy matching による主科目推定の評価（値は表示しない）")
    sp.set_defaults(func=cmd_analyze3)

    sp = sub.add_parser("llm-prepare", help="Phase 4 dry-run: LLM対象選定・payload作成・コスト見積もり（APIは呼ばない）")
    sp.add_argument("--target-fy", type=int, default=2024)
    sp.add_argument("--sample", type=int, default=40)
    sp.add_argument("--all", action="store_true", help="高confidence群以外のすべてを対象にする（実運用パイプライン評価）")
    sp.add_argument("--exclude-e", action="store_true", help="--all でも E（exact・過去1〜2回）は LLM に送らない（A〜D の全件）")
    sp.add_argument("--name", default="dry_run", help="出力サブディレクトリ名")
    sp.add_argument("--reason-category", action="store_true", help="structured output に reason_category を追加")
    sp.set_defaults(func=cmd_llm_prepare)

    sp = sub.add_parser("llm-run", help="Phase 4: dry-run の payload で LLM を呼ぶ（--approve 必須）")
    sp.add_argument("--model", default="claude-opus-5")
    sp.add_argument("--effort", default="medium", choices=["low", "medium", "high"])
    sp.add_argument("--approve", action="store_true", help="コスト見積もりを確認・承認済みであることを示す")
    sp.add_argument("--count-only", action="store_true", help="count_tokens のみ実行して終了")
    sp.add_argument("--name", default="dry_run")
    sp.add_argument("--resume", action="store_true", help="完了済みの明細をスキップして再開")
    sp.add_argument("--max-cost", type=float, default=10.0, help="推定・累計コストの上限（USD）")
    sp.add_argument("--expected-output", type=int, default=250, help="想定 output tokens/件（見積もり用）")
    sp.set_defaults(func=cmd_llm_run)

    sp = sub.add_parser("llm-tokens", help="Phase 4: count_tokens の差分計測で入力トークンの内訳を確認（推論は行わない）")
    sp.add_argument("--models", nargs="+", default=["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"])
    sp.add_argument("--approve", action="store_true")
    sp.set_defaults(func=cmd_llm_tokens)

    sp = sub.add_parser("llm-compare", help="Phase 4: 2モデルの結果を同一サンプルで比較（値は表示しない）")
    sp.add_argument("--a", required=True)
    sp.add_argument("--b", required=True)
    sp.add_argument("--a-label", default="Opus")
    sp.add_argument("--b-label", default="Sonnet")
    sp.set_defaults(func=cmd_llm_compare)

    sp = sub.add_parser("llm-pipeline-eval", help="Phase 4: FY2024 全件の実運用パイプライン評価（値は表示しない）")
    sp.add_argument("--name", default="fy2024_full")
    sp.add_argument("--results", default="results_claude-sonnet-5_medium.jsonl")
    sp.set_defaults(func=cmd_llm_pipeline_eval)

    sp = sub.add_parser("route-eval", help="Phase 4.5: 候補ルーティングの評価（LLM API は呼ばない）")
    sp.add_argument("--name", default="fy2024_full")
    sp.add_argument("--results", default="results_claude-sonnet-5_medium.jsonl")
    sp.set_defaults(func=cmd_route_eval)

    sp = sub.add_parser("llm-eval", help="Phase 4: LLM 結果の評価（正解と照合, 値は表示しない）")
    sp.add_argument("--results", required=True, help="dry_run 内の results_*.jsonl")
    sp.set_defaults(func=cmd_llm_eval)

    sp = sub.add_parser("close-prepare", help="決算処理: 対象抽出・口座系統統合・Amazon除外・層分け・Sonnet payload 作成（APIは呼ばない）")
    sp.add_argument("--fy", type=int, default=2025)
    sp.set_defaults(func=cmd_close_prepare)

    sp = sub.add_parser("close-review", help="決算処理: Sonnet 結果と合わせてレビュー用 CSV を生成（MF write なし）")
    sp.add_argument("--fy", type=int, default=2025)
    sp.add_argument("--results", default="results_claude-sonnet-5_medium.jsonl")
    sp.set_defaults(func=cmd_close_review)

    sp = sub.add_parser("close-groups", help="決算処理: グループ単位レビュー用 review_groups.csv と展開用 members を生成")
    sp.add_argument("--fy", type=int, default=2025)
    sp.add_argument("--results", default="results_claude-sonnet-5_medium.jsonl")
    sp.set_defaults(func=cmd_close_groups)

    sp = sub.add_parser("csv", help="Step 5: CSV変換")
    sp.add_argument("--journals-run", help="仕訳の取得ディレクトリ（既定: 最新）")
    sp.add_argument("--transactions-run", help="明細の取得ディレクトリ（既定: 最新）")
    sp.set_defaults(func=cmd_csv)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    for name in ("urllib3", "httpx", "httpx2", "anthropic"):  # URL等の詳細ログを抑制
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        settings = load_settings(args.env_file)
        args.func(settings, args)
    except DataValidationError as e:
        print(f"[停止] 想定外のデータを検出したため処理を停止しました: {e}", file=sys.stderr)
        return 3
    except ForbiddenRequestError as e:
        print(f"[ブロック] 書き込み防止ガードがリクエストを停止しました: {e}", file=sys.stderr)
        return 2
    except PermissionError as e:
        print(f"[停止] {e}", file=sys.stderr)
        return 4
    except (ConfigError, AuthError, MFApiError, ValueError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    return 0
