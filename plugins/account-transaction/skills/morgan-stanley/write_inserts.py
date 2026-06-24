#!/usr/bin/env python3
"""
write_inserts.py — FALLBACK batch writer for the inserts planned by parse_ms.py.

PRIMARY write path is the ayunit MCP tool `execute_procedure` (cmd='I'), driven by the
morgan-stanley skill: Claude reads <input>.plan.json, duplicate-checks via `execute_select_query`,
canaries one row, then writes the batch. That path needs no .env/venv and is what makes the
folder portable. Use THIS script only as the off-MCP convenience (running inside this repo, or
for a large batch you'd rather loop than issue as individual tool calls).

It reads <input>.plan.json and writes each eligible row to Portfolio.AccountTransaction via
Portfolio.AccountTransaction_Update @CMD='I' over the Ayunit REST execute-procedure endpoint
(same proc/allowlist/contract as the MCP tool — just a different transport, using .env creds).
PRODUCTION — every guard below is on by default.

Safety model
------------
- DRY-RUN by default. Nothing is written unless you pass --confirm.
- Writes every row the parser marked `write: true` — VALIDATED *and* the PENDING rows kept tracked
  on purpose (unresolved -> Asset NULL; unmapped activity -> TransactionType UNKNOWN). Never drop a
  movement that can be written. Only `write: false` rows (lock_blocked / unknown_account /
  zero_amount) are skipped and reported — they're genuinely un-writable. Pass --only-validated to
  restrict to VALIDATED.
- Per-row DUPLICATE PRE-CHECK before every insert
  (account+custody+type+date+Asset+AssetRelated+|value|). AssetRelated is in the key so same-day,
  same-amount coupons from different paying securities aren't mistaken for each other. A hit ->
  skip (idempotent re-runs). Residual: two rows identical on ALL those fields can't be told apart.
- --canary writes only the FIRST eligible row, then stops, so you can eyeball it in the DB first.
- Custody is read per-row from the plan, so this writer is custody-agnostic.

Usage
-----
    PY=../../../../.venv/Scripts/python.exe
    $PY write_inserts.py "Activity.plan.json"                       # dry-run
    $PY write_inserts.py "Activity.plan.json" --account 711-024177-212 --canary --confirm
    $PY write_inserts.py "Activity.plan.json" --account 711-024177-212 --confirm
    $PY write_inserts.py "Activity.plan.json" --bucket trade --confirm
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

DATABASE = "AgnesOrg00DB"
PROC = "Portfolio.AccountTransaction_Update"


def load_env(explicit=None) -> dict:
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


def _post(env, route, body):
    req = urllib.request.Request(
        f"{env['AYUNIT_BASE_URL']}{route}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {env['AYUNIT_API_TOKEN']}",
                 "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read().decode("utf-8"))


def db_query(env, sql, max_rows=50):
    p = _post(env, f"/api/v1/introspection/{DATABASE}/query", {"query": sql, "max_rows": max_rows})
    body = p.get("detail", p)
    if body.get("status") != "success":
        raise RuntimeError(f"query failed: {body.get('error')}")
    return body.get("rows", [])


def exec_proc(env, params):
    return _post(env, f"/api/v1/introspection/{DATABASE}/execute-procedure",
                 {"procedure": PROC, "cmd": "I", "params": params})


def is_dup(env, p) -> bool:
    acct = (p.get("ClientAccount") or "").replace("'", "''")
    cust = (p.get("Custody") or "").replace("'", "''")
    ttype = (p.get("TransactionType") or "").replace("'", "''")
    asset = (p.get("Asset") or "").replace("'", "''")
    related = (p.get("AssetRelated") or "").replace("'", "''")
    val = abs(p.get("Value") or 0)
    sql = (
        "SELECT TOP 1 pk_AccountTransactionID FROM Portfolio.v_AccountTransaction "
        f"WHERE ClientAccount = '{acct}' AND Custody = '{cust}' "
        f"AND TransactionType = '{ttype}' AND Date = '{p['Date']}' "
        f"AND ISNULL(Asset,'') = '{asset}' AND ISNULL(AssetRelated,'') = '{related}' "
        f"AND ABS(ABS(ISNULL(Value,0)) - {val}) <= 0.01 AND Status <> 'IGNORED'"
    )
    return len(db_query(env, sql, max_rows=1)) > 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("plan")
    ap.add_argument("--confirm", action="store_true", help="actually write (else dry-run)")
    ap.add_argument("--canary", action="store_true", help="write only the first eligible row")
    ap.add_argument("--account", default=None, help="restrict to one ClientAccount")
    ap.add_argument("--bucket", default=None, help="restrict to one bucket: trade | cashflow | gl")
    ap.add_argument("--only-validated", action="store_true",
                    help="write ONLY VALIDATED rows; skip the PENDING (unresolved / UNKNOWN) rows the "
                         "plan also marks write:true")
    ap.add_argument("--env", default=None)
    args = ap.parse_args()

    env = load_env(args.env)
    if not env.get("AYUNIT_BASE_URL") or not env.get("AYUNIT_API_TOKEN"):
        sys.exit("AYUNIT_BASE_URL / AYUNIT_API_TOKEN missing from .env")

    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))

    # Eligibility follows the parser's per-row `write` flag: write:true = VALIDATED + the PENDING rows
    # we deliberately keep tracked (unresolved -> Asset NULL; unmapped activity -> UNKNOWN). write:false
    # = lock_blocked / unknown_account / zero_amount (un-writable; the proc rejects them or there's no
    # account/value). Older plans without the flag fall back to the legacy VALIDATED-only rule.
    IGNORE_BUCKETS = ("review", "lock_blocked", "unresolved", "unknown_account", "zero_amount")

    def _writable(row):
        if "write" in row:
            ok = bool(row["write"])
        else:
            ok = (row["bucket"] not in IGNORE_BUCKETS
                  and row["params"].get("Status") == "VALIDATED")
        if ok and args.only_validated and row["params"].get("Status") != "VALIDATED":
            return False
        return ok

    eligible = []
    for row in plan:
        p = row["params"]
        if not _writable(row):
            continue
        if args.bucket and row["bucket"] != args.bucket:
            continue
        if args.account and p.get("ClientAccount") != args.account:
            continue
        eligible.append(row)

    mode = "WRITE" if args.confirm else "DRY-RUN"
    print(f"[{mode}] {len(eligible)} eligible rows "
          f"({'VALIDATED only' if args.only_validated else 'write:true, incl. PENDING'}"
          f"{', bucket=' + args.bucket if args.bucket else ''}"
          f"{', account=' + args.account if args.account else ''})")
    if args.canary:
        print("[canary] will stop after the first successful insert")

    written = skipped_dup = failed = 0
    for row in eligible:
        p = dict(row["params"])
        tag = (f"row {row['row']:>3} {(p.get('ClientAccount') or ''):<16} "
               f"{(p.get('TransactionType') or ''):<22} {(p.get('Asset') or ''):<20} "
               f"val={abs(p.get('Value') or 0):,.2f}")
        try:
            if is_dup(env, p):
                print(f"  SKIP(dup)  {tag}")
                skipped_dup += 1
                continue
        except Exception as e:  # noqa: BLE001
            print(f"  WARN dup-check failed for {tag}: {e}")

        if not args.confirm:
            print(f"  would-write {tag}")
            continue

        res = exec_proc(env, p)
        body = res.get("detail", res)
        if body.get("status") == "success":
            print(f"  OK         {tag}  rowcount={body.get('rowcount')}")
            written += 1
            if args.canary:
                print("[canary] one row written — stopping. Verify in the DB, then re-run "
                      "without --canary to write the rest.")
                break
        else:
            print(f"  FAIL       {tag}  -> {body.get('error')}")
            failed += 1

    print(f"\nDone. written={written} skipped_dup={skipped_dup} failed={failed} "
          f"eligible={len(eligible)}")
    if not args.confirm:
        print("DRY-RUN only — nothing was written. Add --confirm (and optionally --canary) to write.")


if __name__ == "__main__":
    main()
