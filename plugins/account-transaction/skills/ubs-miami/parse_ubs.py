#!/usr/bin/env python3
"""
parse_ubs.py — UBS Miami "Investment Activity" export  ->  Portfolio.AccountTransaction insert plan.

Reads a UBS Miami activity export from UBS Online Services — the legacy BIFF `.xls` (all-accounts
dump) or a per-account `.xlsx` (e.g. AE23928.xlsx); both share the same columns. Applies every
field treatment Sten needs (account map, TransactionType, CUSIP->Asset resolution, bond per-100
vs ETF per-share price, OVERNIGHT sweep interest, sign handling), lock-gates against CheckedDate,
and emits a review plan + ready-to-run Portfolio.AccountTransaction_Update payloads.

DB access = single source of truth via the ayunit MCP.
-------------------------------------------------------------------------------------
This script does only LOCAL work (decode the Excel + the offline field treatment). It never
talks to the DB: Claude gathers the lookup rows via the ayunit MCP tool `execute_select_query`
and hands them back with --lookups. It also NEVER writes; the skill writes the plan via the
ayunit MCP `execute_procedure` (Portfolio.AccountTransaction_Update, cmd='I').

Workflow
--------
    python parse_ubs.py "AE23928.xlsx"
        -> writes <file>.lookups_needed.json + prints the 4 SELECTs to run.
    # Claude runs each SELECT via execute_select_query, saves the rows into <file>.lookups.json
    python parse_ubs.py "AE23928.xlsx" --lookups "AE23928.lookups.json"
        -> writes <file>.plan.json (fully resolved). Skill then inserts via execute_procedure.

Requires an Excel reader locally:  python -m pip install xlrd openpyxl
Options: --lookups PATH | --json PATH (plan out)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

# Excel readers are imported lazily per format: xlrd for legacy .xls, openpyxl for .xlsx.
# One-time install:  python -m pip install xlrd openpyxl

# ----------------------------------------------------------------------------- constants
DATABASE = "AgnesOrg00DB"
CUSTODY = "UBS Miami"
CURRENCY = "USD"

# Account Number (as printed in the file) -> ClientAccount (the key in the DB).
# Derived LIVE from Global.v_ClientAccount (Custody='UBS Miami') by account_map_from_rows() — the
# file's "Account Number" equals the Nickname ("AE 22628") and the ClientAccount ("AE22628") is
# the same thing modulo whitespace, so we index both columns under a normalised key. New UBS
# Miami accounts then resolve automatically, with no code edit; a label the book genuinely
# doesn't have is flagged loudly instead of guessed.
#
# ACCOUNT_OVERRIDES is an escape hatch for the rare file label the DB can't match (e.g. a
# custodian relabelling). Keyed by the raw "Account Number" string. Normally stays empty.
ACCOUNT_OVERRIDES: dict[str, str] = {}

# UBS "Activity" -> Sten TransactionType. See mapping.md for the full rationale.
#   trade      : BUY / SELL — Asset must resolve, Price = |Amount|/Qty*100
#   cashflow   : DEPOSIT / WITHDRAW — Asset = USD, Price = 1
#   gl         : GENERAL LEDGER RECEIPT/DELIVERY — Asset = USD, AssetRelated = paying security
#   review     : needs a human decision before it can be written
ACTIVITY_MAP = {
    "BOUGHT":          ("BUY",                     "trade"),
    "SOLD":            ("SELL",                    "trade"),
    "CALL REDEMPTION": ("SELL",                    "trade"),     # early redemption at par
    "DEPOSIT":         ("DEPOSIT",                 "cashflow"),
    "WITHDRAWAL":      ("WITHDRAW",                "cashflow"),
    "INTEREST":        ("GENERAL LEDGER RECEIPT",  "gl"),
    "FEE CHARGE":      ("GENERAL LEDGER DELIVERY", "gl"),
    "CANCEL BUY":      (None,                      "review"),    # reversal — pair/ignore by hand
}

GL_TYPE = {
    "INTEREST":   "INTEREST/DIVIDEND",   # default for INTEREST; sweep interest overrides to OVERNIGHT
    "FEE CHARGE": "FEE",
}

# The UBS Insured Sweep Program is the cash overnight sweep, not a coupon. Its interest is booked
# as a GENERAL LEDGER RECEIPT with GeneralLedgerType='OVERNIGHT' and AssetRelated=NULL (matching
# how XP/BTG remunerated-account interest is typed). Identified by the sweep's ticker/CUSIP or a
# 'SWEEP'/'OVERNIGHT' description — NOT merely by "asset didn't resolve" (an unregistered bond's
# coupon is still INTEREST/DIVIDEND).
SWEEP_SYMBOL = "MMPUPG"
SWEEP_CUSIP = "90499A981"


def _is_overnight_sweep(symbol: str, cusip: str, descr: str) -> bool:
    d = descr.upper()
    return (symbol.upper() == SWEEP_SYMBOL or cusip == SWEEP_CUSIP
            or "SWEEP" in d or "OVERNIGHT" in d)


# ----------------------------------------------------------------------------- file parsing
def _read_grid(path: Path) -> list[list]:
    """Return the first sheet as a list of rows (each a list of cell values).

    Supports both UBS export formats: legacy BIFF `.xls` (via xlrd) and `.xlsx` (via openpyxl).
    Empty cells are normalised to '' so downstream string handling matches across readers.
    """
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        try:
            import openpyxl
        except ImportError:
            sys.exit("openpyxl is required for .xlsx. Install:  python -m pip install openpyxl")
        ws = openpyxl.load_workbook(str(path), data_only=True, read_only=True).worksheets[0]
        return [["" if v is None else v for v in row]
                for row in ws.iter_rows(values_only=True)]
    if suffix == ".xls":
        try:
            import xlrd
        except ImportError:
            sys.exit("xlrd is required for .xls. Install:  python -m pip install xlrd")
        sh = xlrd.open_workbook(str(path)).sheet_by_index(0)
        return [[sh.cell_value(r, c) for c in range(sh.ncols)] for r in range(sh.nrows)]
    sys.exit(f"unsupported file type '{suffix}' (expected .xls or .xlsx)")


def read_xls(path: Path) -> list[dict]:
    grid = _read_grid(path)
    # locate the header row — the .xls export has a "Filtered by ..." banner above it; the
    # per-account .xlsx puts the header on row 0. Either way, find the 'Account Number' row.
    header_row = next(
        i for i, row in enumerate(grid)
        if row and str(row[0]).strip() == "Account Number"
    )
    hdr = [str(v).strip() for v in grid[header_row]]
    rows = []
    for i in range(header_row + 1, len(grid)):
        cells = grid[i]
        rec = {hdr[c]: (cells[c] if c < len(cells) else "") for c in range(len(hdr))}
        if str(rec.get("Account Number", "")).strip():
            rec["_row"] = i + 1  # 1-based for human reference
            rows.append(rec)
    return rows


def iso_date(s) -> str | None:
    if s is None or str(s).strip() == "":
        return None
    if hasattr(s, "strftime"):          # openpyxl may hand back a real datetime/date
        return s.strftime("%Y-%m-%d")
    s = str(s).strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def num(x) -> float | None:
    if x is None or str(x).strip() == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------------- DB lookups
# The DB is the single source of truth, reached through the ayunit MCP. Claude runs the SELECTs
# below via the MCP tool `execute_select_query` and hands the result rows back through
# --lookups <file>.json. The script itself does ZERO network I/O, so it works in any session
# that has the ayunit MCP connected — Python + an Excel reader are all that's needed locally.
# The SQL lives here once (sql_* builders) so the queries are issued consistently.

def _esc(s: str) -> str:
    return str(s).replace("'", "''")


def sql_assetcustody(cusips: list[str]) -> str:
    like = " OR ".join(f"TickerCustody LIKE '%{_esc(c)}%' OR TickerCustody2 LIKE '%{_esc(c)}%'"
                       for c in cusips)
    return ("SELECT Asset, TickerCustody, TickerCustody2, DescriptionCustody, "
            "PriceFactor, PositionFactor FROM Portfolio.v_AssetCustody "
            f"WHERE Custody = '{CUSTODY}' AND ({like})")


def sql_global_assets(cusips: list[str]) -> str:
    like = " OR ".join(f"Cusip LIKE '%{_esc(c)}%' OR Isin LIKE '%{_esc(c)}%'" for c in cusips)
    return ("SELECT Asset, Description, Cusip, Isin, AssetGroup, SecurityType "
            f"FROM Global.v_Asset WHERE {like}")


def sql_accounts() -> str:
    return f"SELECT ClientAccount, Nickname FROM Global.v_ClientAccount WHERE Custody = '{CUSTODY}'"


def sql_locks() -> str:
    return ("SELECT Account, Date FROM Portfolio.v_CheckedDate "
            f"WHERE Custody = '{CUSTODY}' AND Activated = 1")


def lookup_queries(cusips: list[str]) -> dict:
    """The exact SELECTs that fill each field of lookups.json (run these via execute_select_query)."""
    cusips = sorted({c for c in cusips if c})
    return {
        "assets_custody": sql_assetcustody(cusips) if cusips else None,
        "assets_global":  sql_global_assets(cusips) if cusips else None,
        "accounts":       sql_accounts(),
        "locks":          sql_locks(),
    }


# ---- consumers: raw query rows -> the structures transform() needs --------------------------
def asset_index_from_rows(ac_rows: list, ga_rows: list, cusips: list[str]) -> dict:
    """Resolve each CUSIP, first hit wins:
       1. Portfolio.v_AssetCustody (custody-authoritative TickerCustody/TickerCustody2 -> Asset),
       2. Global.v_Asset exact Cusip, 3. Global.v_Asset CUSIP-in-ISIN.
    AssetCustody hits are enriched with Global.v_Asset metadata (AssetGroup/SecurityType) for the
    per-100/per-share fallback, and carry Price/PositionFactor so a non-unity factor is flagged."""
    index: dict[str, dict] = {}
    ga_by_asset = {r["Asset"]: r for r in ga_rows}
    for c in sorted({c for c in cusips if c}):
        ac = next((r for r in ac_rows
                   if (r.get("TickerCustody") or "").strip() == c
                   or (r.get("TickerCustody2") or "").strip() == c), None)
        if ac:
            meta = ga_by_asset.get(ac["Asset"], {})
            index[c] = {"Asset": ac["Asset"],
                        "Description": ac.get("DescriptionCustody") or meta.get("Description"),
                        "AssetGroup": meta.get("AssetGroup"),
                        "SecurityType": meta.get("SecurityType"),
                        "PriceFactor": ac.get("PriceFactor"),
                        "PositionFactor": ac.get("PositionFactor"), "how": "assetcustody"}
            continue
        hit = next((r for r in ga_rows if (r.get("Cusip") or "").strip() == c), None)
        how = "cusip"
        if not hit:
            hit = next((r for r in ga_rows if c in (r.get("Isin") or "")), None)
            how = "isin"
        if hit:
            index[c] = {"Asset": hit["Asset"], "Description": hit.get("Description"),
                        "AssetGroup": hit.get("AssetGroup"), "SecurityType": hit.get("SecurityType"),
                        "PriceFactor": None, "PositionFactor": None, "how": how}
    return index


def _acct_key(s) -> str:
    """Normalise an account label for matching: drop all whitespace, uppercase."""
    return "".join(str(s).split()).upper()


def account_map_from_rows(rows: list) -> dict:
    """{normalised label -> ClientAccount} from v_ClientAccount rows (indexes both
    ClientAccount and Nickname, so 'AE 22628' and 'AE22628' both resolve)."""
    amap: dict[str, str] = {}
    for r in rows:
        ca = r["ClientAccount"]
        for label in (ca, r.get("Nickname")):
            if not label:
                continue
            key = _acct_key(label)
            if key in amap and amap[key] != ca:
                print(f"WARN: account-label collision for '{key}': {amap[key]} vs {ca}")
            amap[key] = ca
    return amap


def locks_from_rows(rows: list) -> dict:
    """{ClientAccount -> latest active 'YYYY-MM-DD' CheckedDate} from v_CheckedDate rows."""
    out: dict[str, str] = {}
    for r in rows:
        d = str(r["Date"])[:10]
        out[r["Account"]] = max(out.get(r["Account"], d), d)
    return out


def build_indexes(lookups: dict, cusips: list[str]):
    """(asset_index, account_map, locks) from a gathered lookups dict (MCP-fetched)."""
    return (
        asset_index_from_rows(lookups.get("assets_custody") or [],
                              lookups.get("assets_global") or [], cusips),
        account_map_from_rows(lookups.get("accounts") or []),
        locks_from_rows(lookups.get("locks") or []),
    )


# ----------------------------------------------------------------------------- transform
def transform(rows: list[dict], asset_index: dict, locks: dict, account_map: dict) -> list[dict]:
    plan = []
    for rec in rows:
        acct_num = str(rec.get("Account Number", "")).strip()
        activity = str(rec.get("Activity", "")).strip().upper()
        cusip = str(rec.get("Cusip", "")).strip()
        symbol = str(rec.get("Symbol", "")).strip()
        descr = str(rec.get("Description", "")).strip()
        date = iso_date(rec.get("Date"))
        qty = num(rec.get("Quantity"))
        price = num(rec.get("Price"))
        amount = num(rec.get("Amount"))

        notes: list[str] = []
        client_account = ACCOUNT_OVERRIDES.get(acct_num) or account_map.get(_acct_key(acct_num))
        if not client_account:
            notes.append(
                f"UNKNOWN account '{acct_num}' — not found in Global.v_ClientAccount "
                f"(Custody={CUSTODY})"
            )

        ttype, bucket = ACTIVITY_MAP.get(activity, (None, "review"))
        if ttype is None and bucket != "review":
            notes.append(f"UNKNOWN activity '{activity}'")
            bucket = "review"

        params: dict = {
            "Date": date,
            "SettlementDate": date,          # UBS activity report carries a single date
            "ClientAccount": client_account,
            "Custody": CUSTODY,
            "TransactionType": ttype,
            "Currency": CURRENCY,
        }

        asset = None
        if bucket == "trade":
            resolved = asset_index.get(cusip)
            if resolved:
                asset = resolved["Asset"]
                how = resolved.get("how")
                if how == "assetcustody":
                    notes.append(f"asset resolved via AssetCustody ({CUSTODY}) ({cusip} -> {asset})")
                elif how == "isin":
                    notes.append(f"asset resolved via ISIN-contains ({cusip} -> {asset})")
                pf, posf = resolved.get("PriceFactor"), resolved.get("PositionFactor")
                if (pf is not None and pf != 1) or (posf is not None and posf != 1):
                    notes.append(f"AssetCustody PriceFactor={pf} / PositionFactor={posf} "
                                 "(≠1) — review scaling before trusting Price/Quantity")
            else:
                notes.append(f"asset NOT resolved for CUSIP '{cusip}' — leaving PENDING for review")
            abs_qty = abs(qty) if qty else None
            abs_val = abs(amount) if amount else None
            # Price scale: bonds/treasuries are quoted per-100 (% of par, ~99.7); ETFs/equities
            # per-share (~7.12). Recompute the *effective* price from the cash (captures
            # fees/accrued, matching how XP/MS feeds store it): raw = |Amount|/Qty.
            #   per-100 instrument -> filePrice/raw ~= 100   -> store raw*100
            #   per-share          -> filePrice/raw ~= 1     -> store raw
            # Primary signal = the file's own Price column; fallback = the resolved AssetGroup.
            eff_price = price
            if abs_qty and abs_val:
                raw = abs_val / abs_qty
                if price and raw:
                    scale = 100.0 if abs(price / raw - 100) < abs(price / raw - 1) else 1.0
                else:
                    grp = (resolved or {}).get("AssetGroup") or ""
                    styp = (resolved or {}).get("SecurityType") or ""
                    per_share = styp == "ETF" or grp in ("Mutual Fund", "Equity")
                    scale = 1.0 if per_share else 100.0
                eff_price = raw * scale
            params.update({
                "AssetCustody": symbol or descr[:200],
                "CustodyIdentifier": cusip,
                "Asset": asset,
                "AssetRelated": asset,
                "Quantity": abs_qty,
                "PriceExFee": eff_price,     # offshore: proc keeps PriceExFee
                "Price": eff_price,
                "ValueGross": abs_val,
                "Value": abs_val,
            })

        elif bucket == "cashflow":
            abs_val = abs(amount) if amount else None
            params.update({
                "Asset": CURRENCY,
                "Quantity": abs_val,
                "Price": 1,
                "Value": abs_val,
                "Obs": descr,
            })

        elif bucket == "gl":
            abs_val = abs(amount) if amount else None
            gl_type = GL_TYPE.get(activity)
            related = asset_index.get(cusip, {}).get("Asset") if cusip else None
            if activity == "INTEREST":
                if _is_overnight_sweep(symbol, cusip, descr):
                    # UBS Insured Sweep Program = cash overnight interest, not a coupon.
                    gl_type = "OVERNIGHT"
                    related = None
                    notes.append("UBS Insured Sweep Program -> OVERNIGHT GL receipt (AssetRelated NULL)")
                elif not related:
                    notes.append("coupon interest with no resolvable security — AssetRelated NULL")
            params.update({
                "GeneralLedgerType": gl_type,
                "GeneralLedgerDescription": descr,
                "Asset": CURRENCY,
                "AssetRelated": related,
                "Quantity": abs_val,
                "Price": 1,
                "Value": abs_val,
            })

        else:  # review (CANCEL BUY / unknown) — keep it; book PENDING/UNKNOWN, never drop it
            notes.append("REVIEW: no automatic mapping — booked PENDING/UNKNOWN for a human to "
                         "pair / set the real type (e.g. CANCEL BUY reversal)")
            abs_val = abs(amount) if amount else None
            related = asset_index.get(cusip, {}).get("Asset") if cusip else None
            # UNKNOWN is the canonical type for an unmapped launch code — always PENDING until a human
            # sets the real type (ayunit://docs/transaction/types). Carry value + raw so nothing is lost.
            params.update({
                "TransactionType": "UNKNOWN",
                "Asset": None,
                "AssetRelated": related,
                "AssetCustody": (symbol or descr[:200]) or None,
                "CustodyIdentifier": cusip or None,
                "Value": abs_val,
                "ValueGross": abs_val,
                "Obs": descr,
            })
            ttype = "UNKNOWN"

        # lock gate
        lock = locks.get(client_account) if client_account else None
        lock_blocked = bool(lock and date and date <= lock)

        # Disposition. Guiding rule (Sten): NEVER drop a movement that *can* be written. A row that
        # only fails mapping/validation is persisted PENDING so it stays tracked in
        # Portfolio.AccountTransaction (Asset NULL and TransactionType UNKNOWN are valid PENDING
        # states — ayunit://docs/transaction/types). Exactly two cases are genuinely un-writable and
        # are therefore the ONLY ones ignored (write=False):
        #   - unknown_account : no ClientAccount FK to attach the row to.
        #   - lock_blocked    : AccountTransaction_Update REJECTS any write (incl. PENDING) whose Date
        #                       is <= the active CheckedDate — a frozen, reconciled period. Ignored by
        #                       design; the row re-enters on a normal future load once the lock
        #                       advances. (ayunit://docs/checkeddate/usage)
        write = True
        if not client_account:
            status, bucket, write = "PENDING", "unknown_account", False
        elif lock_blocked:
            notes.append(f"LOCK-BLOCKED: date {date} <= CheckedDate {lock} for {client_account} "
                         "— IGNORED (frozen reconciled period; not written)")
            status, bucket, write = "PENDING", "lock_blocked", False
        elif bucket == "review":
            status = "PENDING"                                   # UNKNOWN type, written for triage
        elif bucket == "trade" and not asset:
            notes.append(f"asset unresolved for CUSIP '{cusip}' — written PENDING (Asset NULL); "
                         "register the asset, then re-validate")
            status, bucket = "PENDING", "unresolved"
        elif bucket in ("cashflow", "gl") and not (amount and abs(amount) > 0):
            notes.append("zero/blank amount — nothing to book")
            status, bucket, write = "PENDING", "zero_amount", False
        else:
            status = "VALIDATED"
        params["Status"] = status

        # raw payload + agent note
        params["RawTransaction"] = json.dumps(
            {k: rec[k] for k in rec if k != "_row"}, ensure_ascii=False, default=str
        )
        params["SystemCheck"] = f"UBS Miami activity loader: {activity}"
        if write:
            params["AgentCheck"] = (
                f"load {date}: UBS Miami {activity} -> {ttype} [{status}] "
                f"(file row {rec['_row']}) [ubs-miami]"
            )

        plan.append({
            "row": rec["_row"],
            "bucket": bucket,
            "write": write,
            "account_number": acct_num,
            "activity": activity,
            "description": descr,
            "cusip": cusip,
            "file_qty": qty,
            "file_price": price,
            "file_amount": amount,
            "notes": notes,
            "params": {k: v for k, v in params.items() if v is not None},
        })
    return plan


# ----------------------------------------------------------------------------- reporting
def fmt(v, w, right=False):
    s = "" if v is None else (f"{v:,.2f}" if isinstance(v, float) else str(v))
    return s.rjust(w) if right else s.ljust(w)[:w]


def print_plan(plan: list[dict]):
    from collections import Counter
    print("\n=== UBS Miami — proposed AccountTransaction inserts ===\n")
    by_bucket = Counter(p["bucket"] for p in plan)
    by_status = Counter(p["params"].get("Status") for p in plan)
    print("Buckets:", dict(by_bucket))
    print("Status :", dict(by_status))
    # Disposition: WRITE = inserted (VALIDATED + PENDING for unresolved/UNKNOWN);
    # IGNORE = lock_blocked / unknown_account / zero_amount (genuinely un-writable).
    writes = [p for p in plan if p.get("write")]
    ignored = [p for p in plan if not p.get("write")]
    print(f"Write  : {len(writes)} "
          f"({dict(Counter(p['params'].get('Status') for p in writes))})  |  "
          f"Ignore : {len(ignored)} ({dict(Counter(p['bucket'] for p in ignored))})")
    print()
    h = (fmt("Row", 4), fmt("W?", 3), fmt("Acct", 9), fmt("Activity", 16), fmt("Type", 24),
         fmt("Asset", 22), fmt("Qty", 14, True), fmt("Price", 10, True),
         fmt("Value", 16, True), fmt("St", 10), "Notes")
    print(" ".join(h))
    print("-" * 154)
    for p in plan:
        pr = p["params"]
        print(" ".join((
            fmt(p["row"], 4), fmt("W" if p.get("write") else "-", 3),
            fmt(pr.get("ClientAccount"), 9), fmt(p["activity"], 16),
            fmt(pr.get("TransactionType"), 24), fmt(pr.get("Asset"), 22),
            fmt(pr.get("Quantity"), 14, True), fmt(pr.get("Price"), 10, True),
            fmt(pr.get("Value"), 16, True), fmt(pr.get("Status"), 10),
            "; ".join(p["notes"]),
        )))
    # reconciliation: net file Amount vs net signed Value we will book, per account
    print("\n=== Cash reconciliation (sum of file Amount per account) ===")
    acc = {}
    for p in plan:
        a = p["params"].get("ClientAccount") or p["account_number"]
        acc.setdefault(a, 0.0)
        if p["file_amount"]:
            acc[a] += p["file_amount"]
    for a, v in sorted(acc.items()):
        print(f"  {a:10} net file Amount = {v:,.2f}")


def main():
    ap = argparse.ArgumentParser(description="Parse a UBS Miami export into an AccountTransaction "
                                             "insert plan. Local-only; DB access is via the ayunit MCP.")
    ap.add_argument("file", help="UBS Miami export (.xls or .xlsx)")
    ap.add_argument("--lookups", default=None,
                    help="A JSON of DB rows Claude gathered via the ayunit MCP "
                         "(keys: assets_custody, assets_global, accounts, locks). No network used.")
    ap.add_argument("--json", default=None, help="plan output path (default <file>.plan.json)")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        sys.exit(f"file not found: {path}")

    rows = read_xls(path)
    cusips = [str(r.get("Cusip", "")).strip() for r in rows]
    print(f"Parsed {len(rows)} activity rows from {path.name}")

    # --- gather the DB lookups (the one source of truth) via the ayunit MCP -------------------
    if args.lookups:                                   # rows handed in from the MCP
        lookups = json.loads(Path(args.lookups).read_text(encoding="utf-8"))
        print(f"Using MCP-gathered lookups from {args.lookups}")
    else:
        # No lookups yet: emit the exact SELECTs for Claude to run via execute_select_query,
        # then re-run with --lookups. Nothing is resolved or written in this mode.
        needed = lookup_queries(cusips)
        out = path.with_suffix(".lookups_needed.json")
        out.write_text(json.dumps({"queries": needed,
                                    "save_results_as": str(path.with_suffix(".lookups.json")),
                                    "fields": list(needed)},
                                   indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nNo lookups provided. Wrote the queries to run -> {out}\n")
        for field, q in needed.items():
            print(f"# {field}\n{q}\n")
        print("Run each via the ayunit MCP `execute_select_query`, save the result rows into a "
              "JSON keyed by those field names, then re-run:\n"
              f"  parse_ubs.py \"{path.name}\" --lookups \"{path.with_suffix('.lookups.json').name}\"")
        return

    asset_index, account_map, locks = build_indexes(lookups, cusips)
    print(f"Resolved {len(asset_index)} CUSIPs; {len(account_map)} account labels; "
          f"{len(locks)} active locks.")

    plan = transform(rows, asset_index, locks, account_map)
    print_plan(plan)

    out = Path(args.json) if args.json else path.with_suffix(".plan.json")
    out.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote machine-readable plan -> {out}")
    print("Inserts are NOT executed by this script. The ubs-miami skill writes the plan via the "
          "ayunit MCP `execute_procedure` (canary -> confirm -> batch).")


if __name__ == "__main__":
    main()
