#!/usr/bin/env python3
"""
parse_ms.py — Morgan Stanley "All Activity" export -> Portfolio.AccountTransaction insert plan.

Sibling of ../ubs-miami/parse_ubs.py, same architecture and guardrails, adapted to the MS feed.
Reads a Morgan Stanley activity export (.xlsx, sheet "AllActivity"): a banner block, then a
header row starting at "Activity Date", then the rows. Applies every field treatment Sten needs,
resolves each security against Portfolio.v_AssetCustody + Global.v_Asset by CUSIP, lock-gates
against Portfolio.v_CheckedDate, and emits a review plan + ready-to-run
Portfolio.AccountTransaction_Update payloads.

DB access = single source of truth via the Ayunit backend, reached through the MCP.
-------------------------------------------------------------------------------------
Local-only in the normal path: Claude gathers the lookup rows via the ayunit MCP tool
`execute_select_query` and hands them back with --lookups, so the folder works in any VS Code
with the ayunit MCP connected (no .env, no repo venv). It NEVER writes; the skill writes the plan
via the MCP `execute_procedure`.

Workflow (PRIMARY, portable)
----------------------------
    python parse_ms.py "Activity.xlsx"
        -> writes <file>.lookups_needed.json + prints the 4 SELECTs to run.
    # Claude runs each SELECT via execute_select_query, saves the rows into <file>.lookups.json
    python parse_ms.py "Activity.xlsx" --lookups "Activity.lookups.json"
        -> writes <file>.plan.json. Skill then inserts via execute_procedure.

Fallback (this-repo only): python parse_ms.py "Activity.xlsx" --rest   (uses mcp-builder/.env)

Options: --lookups PATH | --rest | --json PATH (plan out) | --account NAME (scope to one) | --env PATH
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

# Excel reader imported lazily: openpyxl for .xlsx (the MS export format).
#   ../../../../.venv/Scripts/python.exe -m pip install openpyxl

# ----------------------------------------------------------------------------- constants
DATABASE = "AgnesOrg00DB"
CUSTODY = "MS"
CURRENCY = "USD"

# Account label override (file "Account" label -> ClientAccount) for the rare case the live
# Global.v_ClientAccount lookup can't match. Normally empty — the map is derived live.
ACCOUNT_OVERRIDES: dict[str, str] = {}

# MS "Activity" -> (TransactionType, bucket). Grounded in the firm's existing MS rows
# (Portfolio.v_AccountTransaction WHERE Custody='MS'): the official loader maps the clear
# investment + external-cash items and leaves the rest UNKNOWN. We mirror that — map the clear
# cases, route the genuinely ambiguous / internal / personal-banking items to `review`
# (PENDING, never auto-booked), exactly like ubs-miami does with CANCEL BUY.
#   trade    : BUY / SELL   — Asset must resolve; Price recomputed from cash
#   cashflow : DEPOSIT / WITHDRAW — Asset = USD, Price = 1 (Tax Withholding adds GL type TAXES)
#   gl       : GENERAL LEDGER RECEIPT/DELIVERY — Asset = USD, AssetRelated = paying security
#   review   : needs a human decision before it can be written
ACTIVITY_MAP = {
    # --- trades ---
    "BOUGHT":                    ("BUY",                     "trade"),
    "CONTRIBUTION":              ("BUY",                     "trade"),   # IRA/Roth contrib invested as a BUY
    "SUBSCRIPTION":              ("BUY",                     "trade"),   # fund subscription
    "DIVIDEND REINVESTMENT":     ("BUY",                     "trade"),   # buy funded by a dividend
    "AUTO BANK PRODUCT DEPOSIT": ("BUY",                     "trade"),   # MSBNA sweep INTO 99YFH93X0
    "BANK PRODUCT DEPOSIT":      ("BUY",                     "trade"),   # non-Auto MSBNA sweep IN
    "SOLD":                      ("SELL",                    "trade"),
    "REDEMPTION":                ("SELL",                    "trade"),   # called/matured, at par
    "BANK PRODUCT WITHDRAWAL":   ("SELL",                    "trade"),   # MSBNA sweep OUT of 99YFH93X0
    # In-kind security transfers between accounts. ASSET RECEIPT/DELIVERY carry NO cash leg — the
    # position pipeline skips the cash side for these types (see ayunit://docs/portfolio-creator,
    # backoffice/decision-tree); Value/ValueGross hold the market value for the asset's
    # accounting/performance only. Field treatment is identical to a trade.
    "TRANSFER INTO ACCOUNT":     ("ASSET RECEIPT",           "trade"),
    "EXCHANGE IN":               ("ASSET RECEIPT",           "trade"),
    "EXCHANGE RECEIVED IN":      ("ASSET RECEIPT",           "trade"),
    "TRANSFER OUT OF ACCOUNT":   ("ASSET DELIVERY",          "trade"),
    "EXCHANGE OUT":              ("ASSET DELIVERY",          "trade"),
    "EXCHANGE DELIVER OUT":      ("ASSET DELIVERY",          "trade"),
    # --- gl ---
    "INTEREST INCOME":           ("GENERAL LEDGER RECEIPT",  "gl"),
    "INTEREST":                  ("GENERAL LEDGER RECEIPT",  "gl"),
    "DIVIDEND":                  ("GENERAL LEDGER RECEIPT",  "gl"),
    "QUALIFIED DIVIDEND":        ("GENERAL LEDGER RECEIPT",  "gl"),      # INTEREST/DIVIDEND
    "REFUND":                    ("GENERAL LEDGER RECEIPT",  "gl"),      # OTHER
    "MISCELLANEOUS INCOME":      ("GENERAL LEDGER RECEIPT",  "gl"),      # OTHER
    "SERVICE FEE":               ("GENERAL LEDGER DELIVERY", "gl"),      # FEE
    "ACCOUNT FEE":               ("GENERAL LEDGER DELIVERY", "gl"),      # FEE
    # --- cashflow (unconditional direction) ---
    "TAX WITHHOLDING":           ("WITHDRAW",                "cashflow"),  # GL type TAXES
    "FUNDS PAID":                ("WITHDRAW",                "cashflow"),
    "FUNDS DISBURSED":           ("WITHDRAW",                "cashflow"),
    # --- review (do NOT auto-book): genuinely ambiguous ---
    "ZELLE PAYMENT":             (None, "review"),   # personal banking
    "SOLD - ADJUSTED":           (None, "review"),   # correction; Price often 0
    # Any activity starting with "PENDING " (e.g., PENDING CARD TRANS, PENDING CASH) is routed to
    # the `skip` bucket — these are MS-side unconfirmed placeholders that will be re-inserted or
    # deleted once the underlying transaction settles, so we ignore them entirely. Handled in
    # transform() rather than ACTIVITY_MAP so it stays a single rule.
}

# Activities whose cash direction comes from the Amount sign (out -> WITHDRAW, in -> DEPOSIT).
# Routed to the `cashflow` bucket regardless of any ACTIVITY_MAP entry. Use for any MS activity
# where the label alone doesn't tell us the direction — `CASH TRANSFER`, fund movements, personal
# banking, fee adjustments. Sign is the source of truth.
SIGN_CASHFLOW = {
    "CASH TRANSFER",            # inter-account cash move ("FUNDS TRANSFERRED To/From XXX")
    "FUNDS TRANSFERRED",        # was unconditional WITHDRAW — sign is safer than label
    "FUNDS RECEIVED",           # was unconditional DEPOSIT — sign is safer than label
    "DEBIT CARD",               # CashPlus/checking personal banking
    "ATM WITHDRAWAL",
    "SERVICE FEE ADJ",          # sign varies (credit/debit)
    "ONLINE TRANSFER",
    "AUTOMATED PAYMENT",
    "AUTOMATIC DEPOSIT",
    "FX CASH WITHDRAWAL",
}

# Activities whose GL direction comes from the Amount sign (out -> GL DELIVERY, in -> GL RECEIPT).
# Routed to the `gl` bucket regardless of any ACTIVITY_MAP entry. Plus a catch-all in transform():
# any unmapped activity whose name contains "INTEREST" (e.g., "INTEREST INCOME-ADJ",
# "MARGIN INTEREST CHARGED") is sign-directed the same way.
SIGN_GL = {
    "WRITE OFF",                # adjustment; sign varies (DEBIT vs CREDIT)
    "INTEREST INCOME-ADJ",      # interest income adjustment
    "MARGIN INTEREST CHARGED",  # margin debit
}

# GeneralLedgerType per activity (for the gl bucket + Tax Withholding).
GL_TYPE = {
    "INTEREST INCOME": "INTEREST/DIVIDEND", "INTEREST": "INTEREST/DIVIDEND",
    "DIVIDEND": "INTEREST/DIVIDEND", "QUALIFIED DIVIDEND": "INTEREST/DIVIDEND",
    "INTEREST INCOME-ADJ": "INTEREST/DIVIDEND", "MARGIN INTEREST CHARGED": "INTEREST/DIVIDEND",
    "REFUND": "OTHER", "MISCELLANEOUS INCOME": "OTHER", "WRITE OFF": "OTHER",
    "SERVICE FEE": "FEE", "ACCOUNT FEE": "FEE",
    "TAX WITHHOLDING": "TAXES",
}

# The MSBNA PWM Preferred Savings sweep: its "Interest Income" is cash overnight interest, booked
# GENERAL LEDGER RECEIPT / OVERNIGHT with AssetRelated NULL (not a coupon). Detected by description.
def _is_overnight_sweep(descr: str, cusip: str) -> bool:
    d = descr.upper()
    return ("PREFERRED SAVINGS" in d or "MSBNA" in d or "SWEEP" in d or "OVERNIGHT" in d
            or cusip == "99YFH93X0")


# ----------------------------------------------------------------------------- .env / REST
def load_env(explicit: str | None) -> dict:
    path = Path(explicit) if explicit else next(
        (p / ".env" for p in Path(__file__).resolve().parents if (p / ".env").exists()), None)
    env = {}
    if path and path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def db_query(env: dict, sql: str, max_rows: int = 1000) -> list[dict]:
    base, tok = env.get("AYUNIT_BASE_URL"), env.get("AYUNIT_API_TOKEN")
    if not base or not tok:
        raise RuntimeError("AYUNIT_BASE_URL / AYUNIT_API_TOKEN not found in .env")
    req = urllib.request.Request(
        f"{base}/api/v1/introspection/{DATABASE}/query",
        data=json.dumps({"query": sql, "max_rows": max_rows}).encode("utf-8"),
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("status") != "success":
        raise RuntimeError(f"query failed: {payload.get('error')}")
    return payload.get("rows", [])


# ----------------------------------------------------------------------------- file parsing
def _read_grid(path: Path) -> list[list]:
    if path.suffix.lower() != ".xlsx":
        sys.exit(f"unsupported file type '{path.suffix}' (MS exports are .xlsx)")
    try:
        import openpyxl
    except ImportError:
        sys.exit("openpyxl is required. Install: python -m pip install openpyxl")
    ws = openpyxl.load_workbook(str(path), data_only=True, read_only=True).worksheets[0]
    return [["" if v is None else v for v in row] for row in ws.iter_rows(values_only=True)]


def _banner_account(grid: list[list], header_row: int) -> str | None:
    """The single-account files name the account in a banner: 'Account Activity for <X> from ...'.
    Returns <X> when it's a real account (not 'All Accounts'), else None."""
    for r in grid[:header_row]:
        for cell in r:
            s = str(cell or "")
            if "Account Activity for" in s:
                mid = s.split("Account Activity for", 1)[1].split(" from ", 1)[0].strip()
                return None if mid.lower().startswith("all accounts") else mid
    return None


def read_ms(path: Path) -> list[dict]:
    grid = _read_grid(path)
    header_row = next(i for i, row in enumerate(grid)
                      if row and str(row[0]).strip() == "Activity Date")
    hdr = [str(v).strip() for v in grid[header_row]]
    banner_acct = _banner_account(grid, header_row)
    rows = []
    for i in range(header_row + 1, len(grid)):
        cells = grid[i]
        rec = {hdr[c]: (cells[c] if c < len(cells) else "") for c in range(len(hdr))}
        activity = str(rec.get("Activity", "")).strip()
        if not activity:                       # skip blank / summary rows
            continue
        # Account: per-row column when present (all-accounts export), else the banner account.
        rec["_Account"] = str(rec.get("Account", "")).strip() or (banner_acct or "")
        rec["_row"] = i + 1
        rows.append(rec)
    return rows


def iso_date(s) -> str | None:
    if s is None or str(s).strip() == "":
        return None
    if hasattr(s, "strftime"):
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
# Single source of the lookup SQL — run via the MCP `execute_select_query` (PRIMARY) or REST
# (--rest fallback). Same SQL, same Ayunit backend; the rows feed the _from_rows() consumers.
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
    cusips = sorted({c for c in cusips if c})
    return {
        "assets_custody": sql_assetcustody(cusips) if cusips else None,
        "assets_global":  sql_global_assets(cusips) if cusips else None,
        "accounts":       sql_accounts(),
        "locks":          sql_locks(),
    }


def asset_index_from_rows(ac_rows: list, ga_rows: list, cusips: list[str]) -> dict:
    """Resolve each CUSIP: 1) v_AssetCustody (custody-authoritative), 2) Global.v_Asset exact
    Cusip, 3) Global.v_Asset CUSIP-in-ISIN. Enrich with AssetGroup/SecurityType for the price
    fallback; carry Price/PositionFactor."""
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
                        "AssetGroup": meta.get("AssetGroup"), "SecurityType": meta.get("SecurityType"),
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
    return "".join(str(s).split()).upper()


def account_map_from_rows(rows: list) -> dict:
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
    out: dict[str, str] = {}
    for r in rows:
        d = str(r["Date"])[:10]
        out[r["Account"]] = max(out.get(r["Account"], d), d)
    return out


def build_indexes(lookups: dict, cusips: list[str]):
    return (asset_index_from_rows(lookups.get("assets_custody") or [],
                                  lookups.get("assets_global") or [], cusips),
            account_map_from_rows(lookups.get("accounts") or []),
            locks_from_rows(lookups.get("locks") or []))


def fetch_lookups_via_rest(env: dict, cusips: list[str]) -> dict:
    q = lookup_queries(cusips)
    return {
        "assets_custody": db_query(env, q["assets_custody"], 2000) if q["assets_custody"] else [],
        "assets_global":  db_query(env, q["assets_global"], 2000) if q["assets_global"] else [],
        "accounts":       db_query(env, q["accounts"], 2000),
        "locks":          db_query(env, q["locks"], 2000),
    }


# ----------------------------------------------------------------------------- transform
def transform(rows: list[dict], asset_index: dict, locks: dict, account_map: dict) -> list[dict]:
    plan = []
    for rec in rows:
        acct_label = rec.get("_Account", "")
        activity = str(rec.get("Activity", "")).strip().upper()
        cusip = str(rec.get("Cusip", "")).strip()
        symbol = str(rec.get("Symbol", "")).strip()
        descr = " ".join(str(rec.get("Description", "")).split())   # flatten multi-line
        # Date = trade date (Transaction Date); SettlementDate = posting date (Activity Date).
        date = iso_date(rec.get("Transaction Date")) or iso_date(rec.get("Activity Date"))
        settle = iso_date(rec.get("Activity Date")) or date
        qty = num(rec.get("Quantity"))
        price = num(rec.get("Price($)"))
        amount = num(rec.get("Amount($)"))

        notes: list[str] = []
        client_account = ACCOUNT_OVERRIDES.get(acct_label) or account_map.get(_acct_key(acct_label))
        if not client_account:
            notes.append(f"UNKNOWN account '{acct_label}' — not in Global.v_ClientAccount "
                         f"(Custody={CUSTODY}); likely a checking/CashPlus account not in the book")

        ttype, bucket = ACTIVITY_MAP.get(activity, (None, "review"))
        if activity in SIGN_CASHFLOW:               # direction from the Amount sign
            bucket = "cashflow"
            ttype = "WITHDRAW" if (amount or 0) < 0 else "DEPOSIT"
        elif activity in SIGN_GL or (activity not in ACTIVITY_MAP and "INTEREST" in activity):
            # GL direction from the Amount sign. The "INTEREST" catch-all picks up any unmapped
            # interest-related activity (e.g., MARGIN INTEREST CHARGED, INTEREST INCOME-ADJ).
            bucket = "gl"
            ttype = "GENERAL LEDGER DELIVERY" if (amount or 0) < 0 else "GENERAL LEDGER RECEIPT"
        elif activity.startswith("PENDING "):
            # MS-side unconfirmed placeholders — re-inserted or deleted once settled. Skip entirely.
            bucket = "pending_skip"
            ttype = None
        if ttype is None and bucket not in ("review", "pending_skip"):
            notes.append(f"UNKNOWN activity '{activity}'")
            bucket = "review"

        params: dict = {"Date": date, "SettlementDate": settle, "ClientAccount": client_account,
                        "Custody": CUSTODY, "TransactionType": ttype, "Currency": CURRENCY}
        asset = None

        if bucket == "trade":
            resolved = asset_index.get(cusip)
            if resolved:
                asset = resolved["Asset"]
                if resolved.get("how") == "assetcustody":
                    notes.append(f"asset resolved via AssetCustody ({CUSTODY}) ({cusip} -> {asset})")
                elif resolved.get("how") == "isin":
                    notes.append(f"asset resolved via ISIN-contains ({cusip} -> {asset})")
            else:
                notes.append(f"asset NOT resolved for CUSIP '{cusip}' — leaving PENDING for review")
            abs_qty = abs(qty) if qty else None
            abs_val = abs(amount) if amount else None
            # Effective price from cash; bonds per-100 (~99.7), equities/ETFs per-share. Decide
            # scale from the file's own Price; fallback to AssetGroup/SecurityType.
            eff_price = price
            if abs_qty and abs_val:
                raw = abs_val / abs_qty
                if price and raw:
                    scale = 100.0 if abs(price / raw - 100) < abs(price / raw - 1) else 1.0
                else:
                    grp = (resolved or {}).get("AssetGroup") or ""
                    styp = (resolved or {}).get("SecurityType") or ""
                    scale = 1.0 if (styp == "ETF" or grp in ("Mutual Fund", "Equity")) else 100.0
                eff_price = raw * scale
            params.update({"AssetCustody": symbol or descr[:200], "CustodyIdentifier": cusip,
                           "Asset": asset, "AssetRelated": asset, "Quantity": abs_qty,
                           "PriceExFee": eff_price, "Price": eff_price,
                           "ValueGross": abs_val, "Value": abs_val})

        elif bucket == "cashflow":
            abs_val = abs(amount) if amount else None
            related = asset_index.get(cusip, {}).get("Asset") if cusip else None
            params.update({"Asset": CURRENCY, "Quantity": abs_val, "Price": 1, "Value": abs_val,
                           "Obs": descr})
            if activity == "TAX WITHHOLDING":          # WITHDRAW carrying a GL type + the taxed security
                params["GeneralLedgerType"] = "TAXES"
                if related:
                    params["AssetRelated"] = related

        elif bucket == "gl":
            abs_val = abs(amount) if amount else None
            gl_type = GL_TYPE.get(activity)
            # Catch-all "INTEREST" sign-directed gl entries (e.g., MARGIN INTEREST CHARGED) default
            # to INTEREST/DIVIDEND so they flow with other coupon/interest GL movements.
            if gl_type is None and activity in SIGN_GL:
                gl_type = "INTEREST/DIVIDEND" if "INTEREST" in activity else "OTHER"
            elif gl_type is None and "INTEREST" in activity:
                gl_type = "INTEREST/DIVIDEND"
            related = asset_index.get(cusip, {}).get("Asset") if cusip else None
            if activity in ("INTEREST INCOME", "INTEREST", "DIVIDEND") and _is_overnight_sweep(descr, cusip):
                gl_type, related = "OVERNIGHT", None
                notes.append("MSBNA Preferred Savings sweep -> OVERNIGHT GL receipt (AssetRelated NULL)")
            elif gl_type == "INTEREST/DIVIDEND" and not related:
                notes.append("interest/dividend with no resolvable security — AssetRelated NULL")
            params.update({"GeneralLedgerType": gl_type, "GeneralLedgerDescription": descr,
                           "Asset": CURRENCY, "AssetRelated": related,
                           "Quantity": abs_val, "Price": 1, "Value": abs_val})

        elif bucket == "pending_skip":
            # PENDING-prefixed activities: MS-side unconfirmed placeholders. Skipped entirely
            # (write=False below) — not booked in any form. Will reappear once MS settles.
            notes.append(f"PENDING ('{activity}') — unconfirmed MS placeholder; skipped (write=False)")

        else:  # review — activity not in the map: persist as a PENDING/UNKNOWN row, never drop it
            notes.append(f"REVIEW ('{activity}') — no automatic mapping; booked PENDING/UNKNOWN "
                         "for a human to triage (in-kind transfer / internal sweep / personal banking)")
            abs_val = abs(amount) if amount else None
            related = asset_index.get(cusip, {}).get("Asset") if cusip else None
            # UNKNOWN is the canonical type for an unmapped launch code — always PENDING until a human
            # sets the real type (ayunit://docs/transaction/types). Carry value + raw so nothing is lost.
            params.update({"TransactionType": "UNKNOWN", "Asset": None, "AssetRelated": related,
                           "AssetCustody": (symbol or descr[:200]) or None,
                           "CustodyIdentifier": cusip or None,
                           "Value": abs_val, "ValueGross": abs_val, "Obs": descr})
            ttype = "UNKNOWN"

        # lock gate (both Date and SettlementDate must clear the active lock)
        lock = locks.get(client_account) if client_account else None
        earliest = min([d for d in (date, settle) if d], default=None)
        lock_blocked = bool(lock and earliest and earliest <= lock)

        # Disposition. Guiding rule (Sten): NEVER drop a movement that *can* be written. A row that
        # only fails mapping/validation is persisted PENDING so it stays tracked in
        # Portfolio.AccountTransaction (Asset NULL and TransactionType UNKNOWN are valid PENDING
        # states — ayunit://docs/transaction/types). Exactly two cases are genuinely un-writable and
        # are therefore the ONLY ones ignored (write=False):
        #   - unknown_account : no ClientAccount FK to attach the row to.
        #   - lock_blocked    : AccountTransaction_Update REJECTS any write (incl. PENDING) whose Date
        #                       OR SettlementDate is <= the active CheckedDate — a frozen, reconciled
        #                       period. Ignored by design; the row re-enters on a normal future load
        #                       once the lock advances. (ayunit://docs/checkeddate/usage)
        write = True
        if bucket == "pending_skip":
            status, write = "IGNORED", False
        elif not client_account:
            status, bucket, write = "PENDING", "unknown_account", False
        elif lock_blocked:
            notes.append(f"LOCK-BLOCKED: {earliest} <= CheckedDate {lock} for {client_account} "
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

        params["RawTransaction"] = json.dumps({k: rec[k] for k in rec if not k.startswith("_")},
                                              ensure_ascii=False, default=str)
        params["SystemCheck"] = f"MS activity loader: {activity}"
        if write:
            params["AgentCheck"] = (f"load {date}: MS {activity} -> {ttype} [{status}] "
                                    f"(file row {rec['_row']}) [morgan-stanley]")

        plan.append({"row": rec["_row"], "bucket": bucket, "write": write, "account_label": acct_label,
                     "activity": activity, "description": descr, "cusip": cusip,
                     "file_qty": qty, "file_price": price, "file_amount": amount,
                     "notes": notes, "params": {k: v for k, v in params.items() if v is not None}})
    return plan


# ----------------------------------------------------------------------------- reporting
def fmt(v, w, right=False):
    s = "" if v is None else (f"{v:,.2f}" if isinstance(v, float) else str(v))
    return s.rjust(w) if right else s.ljust(w)[:w]


def print_plan(plan: list[dict]):
    from collections import Counter
    print("\n=== Morgan Stanley — proposed AccountTransaction inserts ===\n")
    print("Buckets:", dict(Counter(p["bucket"] for p in plan)))
    print("Status :", dict(Counter(p["params"].get("Status") for p in plan)))
    # Disposition: WRITE = inserted (VALIDATED + PENDING for unresolved/UNKNOWN);
    # IGNORE = lock_blocked / unknown_account / zero_amount (genuinely un-writable).
    writes = [p for p in plan if p.get("write")]
    ignored = [p for p in plan if not p.get("write")]
    print(f"Write  : {len(writes)} "
          f"({dict(Counter(p['params'].get('Status') for p in writes))})  |  "
          f"Ignore : {len(ignored)} ({dict(Counter(p['bucket'] for p in ignored))})")
    print()
    print(" ".join((fmt("Row", 4), fmt("W?", 3), fmt("Account", 14), fmt("Activity", 22),
                    fmt("Type", 22), fmt("Asset", 20), fmt("Value", 16, True), fmt("St", 9),
                    "Notes")))
    print("-" * 154)
    for p in plan:
        pr = p["params"]
        print(" ".join((fmt(p["row"], 4), fmt("W" if p.get("write") else "-", 3),
                        fmt(pr.get("ClientAccount") or p["account_label"], 14),
                        fmt(p["activity"], 22), fmt(pr.get("TransactionType"), 22),
                        fmt(pr.get("Asset"), 20), fmt(pr.get("Value"), 16, True),
                        fmt(pr.get("Status"), 9), "; ".join(p["notes"])[:60])))
    print("\n=== Cash reconciliation (sum of file Amount per resolved account) ===")
    acc = {}
    for p in plan:
        a = p["params"].get("ClientAccount") or f"(unmapped) {p['account_label']}"
        acc[a] = acc.get(a, 0.0) + (p["file_amount"] or 0)
    for a, v in sorted(acc.items()):
        print(f"  {a:32} net file Amount = {v:,.2f}")


def main():
    ap = argparse.ArgumentParser(description="Parse a Morgan Stanley export into an "
                                             "AccountTransaction insert plan. DB via the MCP.")
    ap.add_argument("file", help="MS export (.xlsx)")
    ap.add_argument("--lookups", default=None, help="PRIMARY: JSON of DB rows gathered via the MCP "
                    "(keys: assets_custody, assets_global, accounts, locks). No network used.")
    ap.add_argument("--rest", action="store_true", help="FALLBACK (this-repo): fetch lookups over "
                    "REST using .env. Bypasses the MCP.")
    ap.add_argument("--account", default=None, help="keep only this account label / ClientAccount")
    ap.add_argument("--json", default=None, help="plan output path (default <file>.plan.json)")
    ap.add_argument("--env", default=None, help="path to .env (for --rest)")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        sys.exit(f"file not found: {path}")
    rows = read_ms(path)
    cusips = [str(r.get("Cusip", "")).strip() for r in rows]
    print(f"Parsed {len(rows)} activity rows from {path.name}")

    if args.lookups:
        lookups = json.loads(Path(args.lookups).read_text(encoding="utf-8"))
        print(f"Using MCP-gathered lookups from {args.lookups}")
    elif args.rest:
        lookups = fetch_lookups_via_rest(load_env(args.env), cusips)
        print("Fetched lookups over REST (.env) — fallback path, MCP bypassed.")
    else:
        needed = lookup_queries(cusips)
        out = path.with_suffix(".lookups_needed.json")
        out.write_text(json.dumps({"queries": needed,
                                   "save_results_as": str(path.with_suffix(".lookups.json")),
                                   "fields": list(needed)}, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        print(f"\nNo lookups provided. Wrote the queries to run -> {out}\n")
        for field, qy in needed.items():
            print(f"# {field}\n{qy}\n")
        print("Run each via the ayunit MCP `execute_select_query`, save the rows into a JSON keyed "
              f"by those field names, then re-run:\n  parse_ms.py \"{path.name}\" "
              f"--lookups \"{path.with_suffix('.lookups.json').name}\"")
        return

    asset_index, account_map, locks = build_indexes(lookups, cusips)
    print(f"Resolved {len(asset_index)} CUSIPs; {len(account_map)} account labels; "
          f"{len(locks)} active locks.")

    plan = transform(rows, asset_index, locks, account_map)
    if args.account:
        key = _acct_key(args.account)
        plan = [p for p in plan if _acct_key(p["account_label"]) == key
                or p["params"].get("ClientAccount") == args.account]
        print(f"Scoped to account '{args.account}': {len(plan)} rows")
    print_plan(plan)

    out = Path(args.json) if args.json else path.with_suffix(".plan.json")
    out.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote machine-readable plan -> {out}")
    print("Inserts are NOT executed by this script. The morgan-stanley skill writes the plan via "
          "the MCP `execute_procedure` (canary -> confirm -> batch).")


if __name__ == "__main__":
    main()
