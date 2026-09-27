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
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from . import endpoints as ep
from .auth import AuthError, TokenProvider
from .client import MFAccountingClient, MFApiError
from .config import ConfigError, Settings, load_settings
from .guard import ForbiddenRequestError, GuardedSession
from .inspect_json import format_summary, summarize, value_counts
from . import phase2
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

    sp = sub.add_parser("csv", help="Step 5: CSV変換")
    sp.add_argument("--journals-run", help="仕訳の取得ディレクトリ（既定: 最新）")
    sp.add_argument("--transactions-run", help="明細の取得ディレクトリ（既定: 最新）")
    sp.set_defaults(func=cmd_csv)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    logging.getLogger("urllib3").setLevel(logging.WARNING)  # URL等の詳細ログを抑制
    try:
        settings = load_settings(args.env_file)
        args.func(settings, args)
    except DataValidationError as e:
        print(f"[停止] 想定外のデータを検出したため処理を停止しました: {e}", file=sys.stderr)
        return 3
    except ForbiddenRequestError as e:
        print(f"[ブロック] 書き込み防止ガードがリクエストを停止しました: {e}", file=sys.stderr)
        return 2
    except (ConfigError, AuthError, MFApiError, ValueError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    return 0
