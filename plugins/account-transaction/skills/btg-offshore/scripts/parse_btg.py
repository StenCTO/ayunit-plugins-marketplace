#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
parse_btg.py - LOCAL-ONLY parser for BTG Offshore (Cayman) TRADE movements -> Portfolio.AccountTransaction.

Cowork / ayunit-MCP port of the Routines/enrichment "BTG Offshore" transaction pipeline
(mapper.py + treat_btg_offshore_transaction.py). It never touches the DB: the SKILL.md drives the
ayunit MCP reads (get_btg_offshore_trades + execute_select_query) and the atomic execute_batch write
around it. This script only maps the feed + injected lookups into a review-able plan and the
ready-to-commit execute_batch items.

Field-treatment contract: ../references/mapping.md  (this file is its executable encoding; if they
disagree, fix BOTH together).

Stages
------
Pass 1 (discover):  python parse_btg.py raw1.json raw2.json ...
    -> writes <base>.lookups_needed.json and prints the concrete SELECTs (q1..q10) to run via the MCP.

Pass 1.5 (lookups): python parse_btg.py raw1.json ... --build-lookups
                       --q1 q1.json --q2 q2.json ... --q10 q10.json --out shared.lookups.json
    -> assembles shared.lookups.json from the raw SELECT result files (tolerant of the connector's
       result envelope; no hand-shaping).

Pass 2 (plan):      python parse_btg.py raw1.json ... --lookups shared.lookups.json [--batch OUTDIR]
    -> writes <base>.plan.json + (with --batch) one items_<account>.json per account and manifest.json
       (execute_batch order: TD/NDF registrations -> AssetCustody maps -> AccountTransaction inserts).
"""
import argparse, json, os, re, unicodedata, collections

# --------------------------------------------------------------------------- constants
SOURCE   = "BTG Cayman"
CUSTODY  = "BTG Cayman"
BROKER   = "BTG Cayman"
CURRENCY = "USD"

CONTRACT_SIZE = {"option": 100.0, "bond": 0.01, "td": 0.01, "equity": 1.0, "future": 1.0, "fund": 1.0}
# Non-zero Price sentinel for a zero-Value row: stops the proc's last-price auto-fill (Validator 1.2);
# Validator 1.4 then recomputes Price = ABS(0/(Qty*CS)) = 0, so the row stores an exact Price/Value 0.
ZERO_PRICE_SENTINEL = 1.0

GL_INTEREST = "INTEREST/DIVIDEND"
GL_FEE = "FEE"
GL_TAXES = "TAXES"
GL_OVERNIGHT = "OVERNIGHT"
GL_OTHER = "OTHER"
GL_FORWARD_MATURITY = "Forward Maturity"   # NDF / FX-forward net cash settlement at maturity

ALLOWED_STATUS = {"VALIDATED", "IGNORED", "PENDING", "UPDATED"}
SIGN_BY_TYPE = {"BUY": +1.0, "ASSET RECEIPT": +1.0, "SELL": -1.0, "ASSET DELIVERY": -1.0}

# Sections of the get_btg_offshore_trades feed that carry instrument detail (each is [{trade:[...]}]).
SECURITY_SECTIONS = ["equitiesOptions", "equities", "fixedIncome", "funds", "timeDeposit",
                     "forwards", "futures"]

_INCOME_STOPWORDS = {
    "FUND", "FUNDO", "CLASS", "DIST", "INC", "INCOME", "LTD", "LLC", "CAYMAN", "FDR", "FEEDER",
    "REF", "MASTER", "ACC", "ACCUM", "USD", "BRL", "EUR", "THE", "AND", "OF", "TRADE",
    "CASH", "DIVIDEND", "DIVIDENDS", "DISTRIBUTION", "DISTRIB", "PAYMENT", "COUPON", "INTEREST",
}

# --------------------------------------------------------------------------- io helpers
def load_raw(path):
    obj = json.loads(open(path, encoding="utf-8").read())
    if isinstance(obj, dict) and "result" in obj and isinstance(obj["result"], str):
        obj = json.loads(obj["result"])
    return obj

def load_rows(path):
    """Load a raw execute_select_query result file as a list of dict rows. Tolerates a bare list,
    a {"result":"<json>"} wrapper, or a {columns, rows}/{rows|data|recordset} envelope."""
    obj = json.loads(open(path, encoding="utf-8").read())
    if isinstance(obj, dict) and "result" in obj and isinstance(obj["result"], str):
        obj = json.loads(obj["result"])
    if isinstance(obj, list):
        return [r for r in obj if isinstance(r, dict)]
    if isinstance(obj, dict):
        cols = obj.get("columns") or obj.get("cols")
        for k in ("rows", "data", "recordset", "records", "result"):
            v = obj.get(k)
            if isinstance(v, list):
                if v and isinstance(v[0], dict):
                    return v
                if cols and v and isinstance(v[0], (list, tuple)):
                    names = [c if isinstance(c, str) else (c.get("name") or c.get("Name")) for c in cols]
                    return [dict(zip(names, r)) for r in v]
                return [x for x in v if isinstance(x, dict)]
    return []

def col(row, *names):
    low = {str(k).lower(): v for k, v in row.items()}
    for n in names:
        if n.lower() in low:
            return low[n.lower()]
    return None

def _f(x):
    if x in (None, "", "null"):
        return 0.0
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0

def _date(s):
    return str(s)[:10] if s else None

def _trade_id(item):
    if not isinstance(item, dict):
        return None
    return str(item.get("tradeId") or item.get("tradeID") or item.get("trade_id") or "") or None

# --------------------------------------------------------------------------- feed reading
def entry_records(entry, section):
    """Flatten one section of a trades[] entry: section -> [{trade:[rec,...]}] -> [rec, ...]."""
    out = []
    for block in entry.get(section, []) or []:
        if isinstance(block, dict):
            out += [r for r in block.get("trade", []) if isinstance(r, dict)]
        elif isinstance(block, list):
            out += [r for r in block if isinstance(r, dict)]
    return out

def business_entries(data):
    """The trade entries to book: keep only those whose _operation_date == positionDate, so the
    weekend/holiday repeats (BTG re-reports the last business day's snapshot on Sat/Sun) are dropped."""
    out = []
    for e in data.get("trades", []) or []:
        if not isinstance(e, dict):
            continue
        op = e.get("_operation_date")
        pd_ = e.get("positionDate")
        if op is not None and pd_ is not None and str(op)[:10] != str(pd_)[:10]:
            continue
        out.append(e)
    return out

# --------------------------------------------------------------------------- lookups
class Lookups:
    def __init__(self, d):
        self.assets_custody = {str(k): v for k, v in (d.get("assets_custody") or {}).items()}
        self.assets_isin    = {str(k): v for k, v in (d.get("assets_isin") or {}).items()}
        self.assets_desc    = {str(k): v for k, v in (d.get("assets_desc") or {}).items()}
        self.valid_tt       = set(d.get("valid_tt") or [])
        self.valid_gl       = set(d.get("valid_gl") or [])
        self.locks          = {str(k): str(v)[:10] for k, v in (d.get("locks") or {}).items()}
        self.held_assets    = {str(k): v for k, v in (d.get("held_assets") or {}).items()}
        self.existing_assets = set(d.get("existing_assets") or [])
        self.existing_tx    = d.get("existing_tx") or []
        self.inception_seed = {str(k): v for k, v in (d.get("inception_seed") or {}).items()}
        self.fop_unit_value = {str(k): _f(v) for k, v in (d.get("fop_unit_value") or {}).items()}
        # precompute held-asset description tokens for income resolution
        self._held_tokens = {}
        for acct, lst in self.held_assets.items():
            toks = []
            for pair in lst:
                asset = pair[0] if isinstance(pair, (list, tuple)) else pair.get("Asset")
                desc = pair[1] if isinstance(pair, (list, tuple)) else pair.get("Description")
                toks.append((asset, frozenset(_norm_tokens(desc))))
            self._held_tokens[acct] = toks

    def resolve(self, isin, code):
        """(asset, resolved). AssetCustody by TickerCustody, then Global.v_Asset by ISIN."""
        if code and str(code) in self.assets_custody and self.assets_custody[str(code)]:
            return str(self.assets_custody[str(code)]), True
        if isin and str(isin) in self.assets_isin and self.assets_isin[str(isin)]:
            return str(self.assets_isin[str(isin)]), True
        return None, False

    def by_description(self, desc):
        if not desc:
            return None
        return self.assets_desc.get(str(desc))

# --------------------------------------------------------------------------- income name match
def _norm_tokens(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return [t for t in re.split(r"[^A-Za-z0-9]+", s.upper()) if len(t) >= 3 and t not in _INCOME_STOPWORDS]

def income_security_name(desc):
    parts = [p.strip() for p in re.split(r"\s+-\s+", str(desc or "")) if p.strip()]
    parts = [p for p in parts if not p.upper().replace(" ", "").startswith("TRADEID")]
    if parts and parts[0].upper().lstrip().startswith(("CASH DIVIDEND", "DIVIDEND", "INTEREST", "COUPON")):
        parts = parts[1:]
    return " ".join(parts).strip() or None

def resolve_income_related(name, account, L):
    if not name:
        return None, False
    canon = L.by_description(name)
    if canon:
        return canon, True
    name_tokens = set(_norm_tokens(name))
    if name_tokens:
        best, best_score, second = None, 0, 0
        for cand, desc_tokens in L._held_tokens.get(str(account), []):
            score = len(name_tokens & desc_tokens)
            if score > best_score:
                best, best_score, second = cand, score, best_score
            elif score > second:
                second = score
        if best and best_score >= 2 and best_score > second:
            return best, True
    return None, False

# --------------------------------------------------------------------------- row builder
def _security_direction(td_text, value):
    """SELL on sale/sold/redemption/maturity; BUY on new/purchase/bought/buy; else fall back to the
    cash-value sign (value<0 -> BUY, value>0 -> SELL)."""
    t = str(td_text or "").upper()
    if any(k in t for k in ("SALE", "SOLD", "SELL", "REDEMP", "MATURIT", "MATURITY")):
        return "SELL"
    if any(k in t for k in ("PURCHASE", "BOUGHT", "BUY", "NEW", "SUBSCRIPT")):
        return "BUY"
    if value < 0:
        return "BUY"
    if value > 0:
        return "SELL"
    return None

def row(account, date, settlement, ttype, asset, asset_related, quantity, price, value,
        gl_type=None, gl_desc=None, asset_custody="", custody_identifier="",
        status="VALIDATED", obs="", raw=None, value_gross=None, brokerage_fee=None,
        bucket="", expiry=False, dedup_tid=None):
    """Build an execute_batch item for Portfolio.AccountTransaction_Update @CMD='I' plus parser meta
    (keys prefixed with '_', stripped before commit). Absolute magnitudes are passed; the proc signs
    them from TransactionType. AccountCurrency/AccountFx are omitted (the proc computes them)."""
    if not settlement or (date and settlement < date):
        settlement = date
    vg = value if value_gross is None else value_gross
    params = {
        "Date": date,
        "SettlementDate": settlement,
        "ClientAccount": str(account),
        "Broker": BROKER,
        "Custody": CUSTODY,
        "TransactionType": ttype,
        "Currency": CURRENCY,
        "AssetCustody": asset_custody or "",
        "CustodyIdentifier": str(custody_identifier) if custody_identifier not in (None, "") else "",
        "Asset": asset if asset else "",
        "Quantity": round(abs(quantity), 8),
        "Price": round(abs(price), 8),
        "ValueGross": round(abs(vg), 6),
        "Value": round(abs(value), 6),
        "Status": status,
        "Obs": (obs or "")[:990],
        "RawTransaction": json.dumps(raw, default=str) if raw is not None else None,
    }
    if gl_type is not None:
        params["GeneralLedgerType"] = gl_type
    if gl_desc is not None:
        params["GeneralLedgerDescription"] = (gl_desc or "")[:2990]
    if asset_related is not None:
        params["AssetRelated"] = asset_related
    if brokerage_fee:
        params["BrokerageFee"] = round(abs(brokerage_fee), 6)
    tid = dedup_tid if dedup_tid is not None else _trade_id(raw)
    return {"_params": params, "_bucket": bucket, "_expiry": expiry,
            "_tid": (str(tid) if tid else None),
            "_date": date, "_settle": settlement, "_account": str(account),
            "_ttype": ttype, "_value": round(abs(value), 6), "_signed_value": value,
            "_asset": asset or "", "_custid": str(custody_identifier or ""),
            "_qty": abs(quantity)}

# --------------------------------------------------------------------------- security builder
def security_row(item, L, kind, *, asset_custody, custody_identifier, value_field, asset=None,
                 isin="", obs="", td_text=None, bucket="trade"):
    value = _f(item.get(value_field))
    if value == 0:
        for alt in ("amount", "currentValue", "netValue", "tradeAmountValue"):
            if alt != value_field and _f(item.get(alt)):
                value = _f(item.get(alt)); break
    qty_mag = abs(_f(item.get("quantity")))
    td_text = td_text if td_text is not None else (item.get("tradeDescription") or item.get("eventType") or "")
    ttype = _security_direction(td_text, value)
    fop = False
    if ttype is None:
        q = _f(item.get("quantity"))
        if q == 0:
            return None
        du = (obs or "").upper() + " " + str(td_text).upper()
        if "RECEIVE FREE OF PAYMENT" in du or "RECEIPT FREE OF PAYMENT" in du:
            ttype = "ASSET RECEIPT"
        elif "FREE OF PAYMENT" in du:
            ttype = "ASSET DELIVERY"
        else:
            ttype = "ASSET RECEIPT" if q > 0 else "ASSET DELIVERY"
        fop = True
    elif "FREE OF PAYMENT" in (str(td_text).upper() + " " + (obs or "").upper()):
        ttype = "ASSET RECEIPT" if "RECEIVE" in (str(td_text).upper()+obs.upper()) else "ASSET DELIVERY"
        fop = True

    resolved = True
    if asset is None:
        asset, resolved = L.resolve(isin, custody_identifier)
        if not resolved:
            canon = L.by_description(obs) or L.by_description(item.get("description"))
            if canon:
                asset, resolved = canon, True
        if not resolved:
            asset = ""

    cs = CONTRACT_SIZE.get(kind, 1.0)
    if qty_mag == 0:
        qty_mag, cs = abs(value), 1.0

    if fop:
        unit = L.fop_unit_value.get(str(asset)) if (resolved and asset) else None
        if unit:
            value = qty_mag * unit
            price = unit / cs if cs else unit
            status = "VALIDATED" if resolved else "PENDING"
        else:
            value, price, status = 0.0, ZERO_PRICE_SENTINEL, "PENDING"
        return row(account=str(item.get("accountId") or ""),
                   date=_date(item.get("tradeDate")), settlement=_date(item.get("settlementDate")),
                   ttype=ttype, asset=asset, asset_related=(asset or None),
                   quantity=qty_mag, price=price, value=value,
                   asset_custody=asset_custody, custody_identifier=custody_identifier,
                   status=status, obs=obs, raw=item, bucket=("trade" if status == "VALIDATED" else "unresolved"))

    price = abs(value) / (qty_mag * cs) if (qty_mag and cs) else abs(_f(item.get("price")))
    status = "VALIDATED" if (resolved and qty_mag and price) else "PENDING"
    return row(account=str(item.get("accountId") or ""),
               date=_date(item.get("tradeDate")), settlement=_date(item.get("settlementDate")),
               ttype=ttype, asset=asset, asset_related=(asset or None),
               quantity=qty_mag, price=price, value=value,
               asset_custody=asset_custody, custody_identifier=custody_identifier,
               status=status, obs=obs, raw=item, bucket=(bucket if status == "VALIDATED" else "unresolved"))

# --------------------------------------------------------------------------- per-section handlers
def treat_equities_options(records, L, reg_maps):
    rows = []
    for item in records:
        aid = item.get("assetId")
        td = item.get("tradeDescription") or ""
        obs = " ".join(x for x in (item.get("description") or "", td) if x).strip()
        raw_qty = _f(item.get("quantity"))
        if "EXPIR" in td.upper() and _f(item.get("value")) == 0 and raw_qty != 0:
            asset, resolved = L.resolve("", aid)
            if not resolved:
                canon = L.by_description(item.get("description"))
                if canon:
                    asset, resolved = canon, True
                    reg_maps.append({"ticker": str(aid), "asset": canon})
            erow = row(account=str(item.get("accountId") or ""),
                       date=_date(item.get("tradeDate")), settlement=_date(item.get("settlementDate")),
                       ttype="SELL", asset=(asset if resolved else ""),
                       asset_related=(asset if resolved else None),
                       quantity=abs(raw_qty), price=ZERO_PRICE_SENTINEL, value=0.0, value_gross=0.0,
                       asset_custody=str(aid or ""), custody_identifier=aid,
                       status=("VALIDATED" if resolved else "PENDING"), obs=obs, raw=item,
                       bucket="expiry", expiry=True)
            rows.append(erow)
            continue
        built = security_row(item, L, "option", asset_custody=str(aid or ""), custody_identifier=aid,
                             value_field="value", obs=obs, bucket="trade")
        if not built:
            continue
        if not built["_asset"]:
            canon = L.by_description(item.get("description"))
            if canon:
                reg_maps.append({"ticker": str(aid), "asset": canon})
        rows.append(built)
    return rows

def treat_equities(records, L, reg_maps):
    rows = []
    for item in records:
        isin = item.get("isin") or ""
        code = item.get("assetId") or item.get("assetCode")
        ticker = item.get("ticker") or ""
        desc = item.get("description") or ""
        amount = _f(item.get("amount"))
        base = _f(item.get("baseValue"))
        qty = abs(_f(item.get("quantity")))
        if "DIVIDEND" in desc.upper() or qty == 0:
            cash = amount or base
            if cash == 0:
                continue
            related, resolved = L.resolve(isin, code)
            ttype = "GENERAL LEDGER RECEIPT" if cash > 0 else "GENERAL LEDGER DELIVERY"
            rows.append(row(account=str(item.get("accountId") or ""),
                            date=_date(item.get("tradeDate")), settlement=_date(item.get("settlementDate")),
                            ttype=ttype, asset="USD", asset_related=(related if resolved else None),
                            quantity=cash, price=1.0, value=cash, gl_type=GL_INTEREST,
                            gl_desc=f"{ticker} {desc}".strip(),
                            asset_custody=isin or str(code or ""), custody_identifier=code,
                            status="VALIDATED", obs=f"{ticker} {desc}".strip(), raw=item, bucket="gl"))
            continue
        value = amount or base
        if value == 0 or qty == 0:
            continue
        vgross = base or amount
        fee = abs(abs(amount) - abs(base)) if (amount and base) else None
        ttype = _security_direction(desc, value)
        asset, resolved = L.resolve(isin, code)
        if not resolved:
            asset = ""
        price = abs(value) / qty
        status = "VALIDATED" if (resolved and qty and price) else "PENDING"
        rows.append(row(account=str(item.get("accountId") or ""),
                        date=_date(item.get("tradeDate")), settlement=_date(item.get("settlementDate")),
                        ttype=ttype, asset=asset, asset_related=(asset or None),
                        quantity=qty, price=price, value=value, value_gross=vgross, brokerage_fee=fee,
                        asset_custody=isin or str(code or ""), custody_identifier=code,
                        status=status, obs=f"{ticker} {desc}".strip(), raw=item,
                        bucket=("trade" if status == "VALIDATED" else "unresolved")))
    return rows

def treat_fixed_income(records, L, reg_maps):
    rows = []
    for item in records:
        isin = item.get("isin") or ""
        aid = item.get("assetId")
        desc = item.get("description") or item.get("issuer") or ""
        ev = item.get("eventType") or ""
        if "COUPON" in desc.upper() or "COUPON" in ev.upper():
            cash = _f(item.get("value")) or _f(item.get("baseValue"))
            if cash == 0:
                continue
            related, resolved = L.resolve(isin, aid)
            ttype = "GENERAL LEDGER RECEIPT" if cash > 0 else "GENERAL LEDGER DELIVERY"
            rows.append(row(account=str(item.get("accountId") or ""),
                            date=_date(item.get("tradeDate")), settlement=_date(item.get("settlementDate")),
                            ttype=ttype, asset="USD", asset_related=(related if resolved else None),
                            quantity=cash, price=1.0, value=cash, gl_type=GL_INTEREST, gl_desc=desc,
                            asset_custody=isin or str(aid or ""), custody_identifier=aid,
                            status="VALIDATED", obs=desc, raw=item, bucket="gl"))
            continue
        built = security_row(item, L, "bond", asset_custody=isin or str(aid or ""),
                             custody_identifier=aid, value_field="value", isin=isin, obs=desc,
                             td_text=(item.get("tradeDescription") or ev), bucket="trade")
        if built:
            rows.append(built)
    return rows

def treat_funds(records, L, reg_maps, combo_account):
    rows = []
    for item in records:
        item = dict(item)
        item.setdefault("accountId", combo_account)
        desc = (item.get("description") or "").upper()
        value = _f(item.get("tradeAmountValue"))
        if value == 0:
            continue
        isin = item.get("isin") or ""
        ident = item.get("assetIdentification")
        asset, resolved = L.resolve(isin, ident)
        obs = item.get("fundName") or item.get("description") or str(ident)
        if "INCOME" in desc:
            ttype = "GENERAL LEDGER RECEIPT" if value > 0 else "GENERAL LEDGER DELIVERY"
            rows.append(row(account=str(item.get("accountId") or ""),
                            date=_date(item.get("tradeDate")), settlement=_date(item.get("settlementDate")),
                            ttype=ttype, asset="USD", asset_related=(asset if resolved else None),
                            quantity=value, price=1.0, value=value, gl_type=GL_INTEREST, gl_desc=obs,
                            custody_identifier=ident, asset_custody=isin or str(ident or ""),
                            status="VALIDATED", obs=obs, raw=item, bucket="gl"))
            continue
        built = security_row(item, L, "fund", asset_custody=isin or str(ident or ""),
                             custody_identifier=ident, value_field="tradeAmountValue", isin=isin,
                             asset=(asset if resolved else None), obs=obs,
                             td_text=item.get("tradeDescription"), bucket="trade")
        if built:
            rows.append(built)
    return rows

def treat_time_deposit(records, L, reg_candidates):
    rows = []
    for item in records:
        aid = item.get("assetId")
        canon = f"TD{aid}"
        reg_candidates[canon] = {
            "ticker": str(aid),
            "register": td_register_params(aid, item.get("returnDate"), item.get("rate")),
            "map": assetcustody_map_params(aid, canon),
        }
        built = security_row(item, L, "td", asset_custody=str(aid or ""), custody_identifier=aid,
                             value_field="value", asset=canon,
                             obs=item.get("description") or f"BTG Time Deposit {aid}",
                             td_text=item.get("tradeDescription"), bucket="trade")
        if built:
            rows.append(built)
    return rows

def treat_forwards(records, L, reg_candidates):
    rows = []
    for item in records:
        aid = item.get("assetId")
        canon = f"NDF{aid}"
        pc = item.get("purchaseCurrency") or item.get("longSideCurrency")
        sc = item.get("saleCurrency") or item.get("shortSideCurrency")
        reg_candidates[canon] = {
            "ticker": str(aid),
            "register": ndf_register_params(aid, pc, sc, item),
            "map": assetcustody_map_params(aid, canon),
        }
        account = str(item.get("accountId") or "")
        is_maturity = "MATUR" in (item.get("description") or "").upper() or \
                      "MATUR" in (item.get("tradeDescription") or "").upper()
        if not is_maturity:
            topen = _date(item.get("tradeDate")) or _date(item.get("settlementDate"))
            rows.append(row(account=account, date=topen, settlement=topen, ttype="BUY",
                            asset=canon, asset_related=canon, quantity=1.0,
                            price=ZERO_PRICE_SENTINEL, value=0.0,
                            asset_custody=str(aid or ""), custody_identifier=aid, status="VALIDATED",
                            obs=item.get("description") or f"NDF {aid} New {pc}x{sc}",
                            raw={**item, "tradeId": f"{aid}:NDF_NEW"}, dedup_tid=f"{aid}:NDF_NEW",
                            bucket="ndf"))
            continue
        mat = _date(item.get("settlementDate")) or _date(item.get("fixDate")) or _date(item.get("maturityDate"))
        rows.append(row(account=account, date=mat, settlement=mat, ttype="SELL",
                        asset=canon, asset_related=canon, quantity=1.0,
                        price=ZERO_PRICE_SENTINEL, value=0.0,
                        asset_custody=str(aid or ""), custody_identifier=aid, status="VALIDATED",
                        obs=item.get("description") or f"NDF {aid} Maturity {pc}x{sc}",
                        raw={**item, "tradeId": f"{aid}:NDF_MAT"}, dedup_tid=f"{aid}:NDF_MAT",
                        bucket="ndf"))
    return rows

def treat_futures(records, L, reg_maps):
    rows = []
    for item in records:
        aid = item.get("assetId") or item.get("codTitulo")
        q = _f(item.get("quantity"))
        if q == 0:
            continue
        obs = item.get("assetDescription") or item.get("description") or ""
        td = item.get("tradeDescription") or ""
        asset, resolved = L.resolve("", aid)
        side = "BUY" if q > 0 else "SELL"
        if "SALE" in td.upper() or "SOLD" in td.upper():
            side = "SELL"
        elif "PURCHASE" in td.upper() or "BOUGHT" in td.upper():
            side = "BUY"
        rows.append(row(account=str(item.get("accountId") or ""),
                        date=_date(item.get("tradeDate")), settlement=_date(item.get("settlementDate")),
                        ttype=side, asset=(asset if resolved else ""),
                        asset_related=(asset if resolved else None),
                        quantity=abs(q), price=ZERO_PRICE_SENTINEL, value=0.0,
                        asset_custody=str(aid or ""), custody_identifier=aid,
                        status=("VALIDATED" if resolved else "PENDING"), obs=obs, raw=item,
                        bucket=("future" if resolved else "unresolved")))
    return rows

# --------------------------------------------------------------------------- cash ledger
def classify_cash(item):
    transfer = (item.get("transferType") or "").upper()
    product = (item.get("productType") or "")
    desc_u = (item.get("description") or "").upper()
    value = _f(item.get("value"))
    if transfer == "SECURITY":
        return None
    if product == "StructuredFlows":
        return None
    if desc_u.startswith("MARGIN CALL") or desc_u.startswith("MARGIN RELEASE"):
        return None
    if desc_u.startswith(("CAPITAL CALL", "CAPITAL RETURN", "INCOME")):
        return None
    if product == "FXNDF" and transfer == "PRINCIPAL":
        ttype = "GENERAL LEDGER RECEIPT" if value > 0 else "GENERAL LEDGER DELIVERY"
        return (ttype, GL_FORWARD_MATURITY, "ndf")
    if transfer == "DIVIDEND" or product == "CA" or "DIVIDEND" in desc_u or "DISTRIBUTION" in desc_u:
        ttype = "GENERAL LEDGER RECEIPT" if value > 0 else "GENERAL LEDGER DELIVERY"
        return (ttype, GL_INTEREST, "income")
    if desc_u.startswith("DEPOSIT"):
        return ("DEPOSIT", None, None)
    if desc_u.startswith("CASH WITHDRAWAL") or desc_u.startswith("WITHDRAW"):
        return ("WITHDRAW", None, None)
    if "FEE" in transfer or "FEE" in desc_u or "COMMISSION" in desc_u:
        return ("GENERAL LEDGER DELIVERY", GL_FEE, None)
    if "TAX" in desc_u:
        return ("GENERAL LEDGER DELIVERY", GL_TAXES, None)
    if transfer == "INTEREST" or "INTEREST" in desc_u or product == "InterestBearing":
        ttype = "GENERAL LEDGER RECEIPT" if value > 0 else "GENERAL LEDGER DELIVERY"
        if "DAILY INTEREST" in desc_u:
            return (ttype, GL_OVERNIGHT, None)
        return (ttype, GL_INTEREST, "income" if income_security_name(item.get("description")) else None)
    if product == "CustomerTransfer":
        return ("DEPOSIT", None, None) if value > 0 else ("WITHDRAW", None, None)
    return None

def treat_cash(records, L):
    rows = []
    for item in records:
        cls = classify_cash(item)
        if not cls:
            continue
        ttype, gl_type, kind = cls
        value = _f(item.get("value"))
        if value == 0:
            continue
        account = str(item.get("accountId") or "")
        trade, settle = _date(item.get("tradeDate")), _date(item.get("settlementDate"))
        tx_date = trade or settle
        asset_related, status = None, "VALIDATED"
        bucket = "cashflow" if ttype in ("DEPOSIT", "WITHDRAW") else "gl"
        if kind == "ndf":
            tid = item.get("tradeId")
            asset_related = f"NDF{tid}" if tid else None
            tx_date = settle or trade
        elif kind == "income":
            asset_related, resolved = resolve_income_related(income_security_name(item.get("description")),
                                                             account, L)
            status = "VALIDATED" if resolved else "PENDING"
            if not resolved:
                bucket = "unresolved"
        rows.append(row(account=account, date=tx_date, settlement=settle or tx_date, ttype=ttype,
                        asset="USD", asset_related=asset_related, quantity=value, price=1.0, value=value,
                        gl_type=gl_type, gl_desc=(item.get("description") if gl_type else None),
                        status=status, obs=item.get("description") or "", raw=item, bucket=bucket))
    return rows

# --------------------------------------------------------------------------- registration params
def td_register_params(asset_id, ret, rate):
    return {"Asset": f"TD{asset_id}", "Description": f"BTG Time Deposit {ret} {rate}",
            "Currency": "USD", "Offshore": 1, "AssetGroup": "Time Deposit", "SecurityType": "TD",
            "Product": "Cash/Cash Equivalent", "AssetClass": "Time Deposit", "ContractSize": 0.01,
            "Activated": 1, "Benchmark": "SOFR", "Source": SOURCE, "Issuer": "Banco BTG Pactual",
            "Maturity": ret}

def ndf_register_params(asset_id, pc, sc, item):
    mat = _date(item.get("settlementDate")) or _date(item.get("maturityDate")) or ""
    return {"Asset": f"NDF{asset_id}", "Description": f"NDF {pc}x{sc} {mat}".strip(),
            "Currency": "USD", "Offshore": 1, "AssetGroup": "FX", "SecurityType": "NDF",
            "Product": "Currencies", "AssetClass": "FX Hedge", "ContractSize": 1, "Activated": 1,
            "Source": SOURCE, "Maturity": mat}

def assetcustody_map_params(ticker, canon):
    return {"TickerCustody": str(ticker), "Asset": canon, "Custody": CUSTODY,
            "PositionFactor": 1, "PriceFactor": 1}

# --------------------------------------------------------------------------- map one entry
def map_entry(entry, L, reg_candidates, reg_maps):
    rows = []
    account = str(entry.get("accountNumber") or "")
    # funds records carry no accountId -> inject the entry's account
    rows += treat_equities_options(entry_records(entry, "equitiesOptions"), L, reg_maps)
    rows += treat_equities(entry_records(entry, "equities"), L, reg_maps)
    rows += treat_fixed_income(entry_records(entry, "fixedIncome"), L, reg_maps)
    rows += treat_futures(entry_records(entry, "futures"), L, reg_maps)
    rows += treat_funds(entry_records(entry, "funds"), L, reg_maps, account)
    rows += treat_time_deposit(entry_records(entry, "timeDeposit"), L, reg_candidates)
    rows += treat_forwards(entry_records(entry, "forwards"), L, reg_candidates)
    rows += treat_cash(entry_records(entry, "cash"), L)
    # ensure account stamped
    for r in rows:
        if not r["_params"]["ClientAccount"]:
            r["_params"]["ClientAccount"] = account
            r["_account"] = account
    return [r for r in rows if r["_params"]["ClientAccount"] and r["_params"]["Date"]]

# --------------------------------------------------------------------------- dedup keys
def dedup_key_from_row(r):
    if r["_tid"]:
        return ("TID", r["_tid"], round(abs(r["_value"]), 2))
    return ("SIG", str(r["_date"])[:10], str(r["_ttype"] or ""), str(r["_custid"] or ""),
            round(r["_signed_value"], 2))

def dedup_key_from_db(rec):
    raw = col(rec, "RawTransaction", "rawtransaction")
    tid = None
    if raw:
        try:
            tid = _trade_id(json.loads(raw))
        except Exception:
            tid = None
    val = _f(col(rec, "Value"))
    if tid:
        return ("TID", str(tid), round(abs(val), 2))
    return ("SIG", str(col(rec, "Date"))[:10], str(col(rec, "TransactionType") or ""),
            str(col(rec, "CustodyIdentifier") or ""), round(val, 2))

# --------------------------------------------------------------------------- expiry side
def signed_qty(ttype, qty):
    s = SIGN_BY_TYPE.get(str(ttype or "").upper())
    return s * abs(_f(qty)) if s else 0.0

def resolve_expiry_sides(account, rows, L):
    expiries = [r for r in rows if r["_expiry"]]
    if not expiries:
        return
    assets = {r["_asset"] for r in expiries if r["_asset"]}
    seed = L.inception_seed.get(str(account), {})
    # net of post-inception legs over DB existing_tx UNION this batch (deduped by key)
    legs = {}   # asset -> {dedup_key: (date, signed_qty)}
    for r in rows:
        if r["_expiry"] or r["_asset"] not in assets:
            continue
        sq = signed_qty(r["_ttype"], r["_qty"])
        if sq:
            legs.setdefault(r["_asset"], {}).setdefault(dedup_key_from_row(r), (str(r["_date"])[:10], sq))
    for rec in L.existing_tx:
        a = str(col(rec, "Asset") or "")
        if a not in assets:
            continue
        sq = signed_qty(col(rec, "TransactionType"), col(rec, "Quantity"))
        if sq:
            legs.setdefault(a, {}).setdefault(dedup_key_from_db(rec),
                                              (str(col(rec, "Date"))[:10], sq))
    for r in expiries:
        a, edate = r["_asset"], str(r["_date"])[:10]
        net = _f(seed.get(a, 0.0)) + sum(sq for (d, sq) in legs.get(a, {}).values() if d < edate)
        if net < 0:
            r["_ttype"] = "BUY"; r["_params"]["TransactionType"] = "BUY"
        elif net > 0:
            r["_ttype"] = "SELL"; r["_params"]["TransactionType"] = "SELL"
        else:
            r["_ttype"] = "SELL"; r["_params"]["TransactionType"] = "SELL"
            r["_params"]["Status"] = "PENDING"; r["_bucket"] = "expiry-pending"
        if r["_params"].get("AssetRelated") is None and r["_params"]["Asset"]:
            r["_params"]["AssetRelated"] = r["_params"]["Asset"]

# --------------------------------------------------------------------------- plan build
def build_plan(data, L, accounts_filter=None):
    entries = business_entries(data)
    reg_candidates, reg_maps = {}, []
    all_rows = []
    for e in entries:
        acct = str(e.get("accountNumber") or "")
        if accounts_filter and acct not in accounts_filter:
            continue
        all_rows += map_entry(e, L, reg_candidates, reg_maps)

    # group by account
    by_acct = collections.defaultdict(list)
    for r in all_rows:
        by_acct[r["_account"]].append(r)

    stats = collections.Counter()
    plan_accounts = {}
    for account, rows in by_acct.items():
        lock = L.locks.get(str(account))
        resolve_expiry_sides(account, rows, L)

        kept, blocked, dropped, dups = [], 0, 0, 0
        seen = set()
        # seed with DB-existing keys for the dedup window
        for rec in L.existing_tx:
            if str(col(rec, "ClientAccount") or col(rec, "Account") or "") == str(account):
                seen.add(dedup_key_from_db(rec))
        for r in rows:
            tt = r["_params"]["TransactionType"]
            gl = r["_params"].get("GeneralLedgerType")
            if L.valid_tt and tt not in L.valid_tt:
                dropped += 1; r["_bucket"] = "dropped_invalid"; continue
            if gl is not None and L.valid_gl and gl not in L.valid_gl:
                dropped += 1; r["_bucket"] = "dropped_invalid"; continue
            if r["_params"]["Status"] not in ALLOWED_STATUS:
                r["_params"]["Status"] = "PENDING"
            d, sd = str(r["_date"])[:10], str(r["_settle"])[:10]
            if lock and (d <= lock or sd <= lock):
                blocked += 1; r["_bucket"] = "lock_blocked"; continue
            key = dedup_key_from_row(r)
            if key in seen:
                dups += 1; r["_bucket"] = "skipped_dup"; continue
            seen.add(key)
            kept.append(r)

        validated = sum(1 for r in kept if r["_params"]["Status"] == "VALIDATED")
        pending = sum(1 for r in kept if r["_params"]["Status"] == "PENDING")
        stats["validated"] += validated; stats["pending"] += pending
        stats["lock_blocked"] += blocked; stats["dropped_invalid"] += dropped; stats["skipped_dup"] += dups
        plan_accounts[account] = {"rows": kept, "validated": validated, "pending": pending,
                                  "lock_blocked": blocked, "dropped_invalid": dropped,
                                  "skipped_dup": dups, "lock": lock}
    return plan_accounts, reg_candidates, reg_maps, stats

# --------------------------------------------------------------------------- batch items
def batch_items_for_account(account, pa, reg_candidates, reg_maps, L, first):
    """Build the execute_batch item list for one account. Registrations + maps go in the FIRST
    account's batch (shared across the run). Returns list of {procedure, cmd, params}."""
    items = []
    if first:
        for canon, cand in reg_candidates.items():
            if canon in L.existing_assets:
                continue
            items.append({"procedure": "Global.Asset_Update", "cmd": "I", "params": cand["register"]})
            if cand["ticker"] not in L.assets_custody:
                items.append({"procedure": "Portfolio.AssetCustody_Update", "cmd": "I",
                              "params": cand["map"]})
        seen_map = set()
        for m in reg_maps:
            if m["ticker"] in L.assets_custody or m["ticker"] in seen_map:
                continue
            seen_map.add(m["ticker"])
            items.append({"procedure": "Portfolio.AssetCustody_Update", "cmd": "I",
                          "params": assetcustody_map_params(m["ticker"], m["asset"])})
    for r in pa["rows"]:
        params = {k: v for k, v in r["_params"].items() if v is not None}
        items.append({"procedure": "Portfolio.AccountTransaction_Update", "cmd": "I", "params": params})
    return items

# --------------------------------------------------------------------------- discovery
def discover(datasets):
    aids, isins, descs, accts, td_ndf = set(), set(), set(), set(), set()
    for data in datasets:
        for e in business_entries(data):
            accts.add(str(e.get("accountNumber") or ""))
            for sec in SECURITY_SECTIONS:
                for t in entry_records(e, sec):
                    a = t.get("assetId") or t.get("assetIdentification") or t.get("assetCode") or t.get("codTitulo")
                    if a:
                        aids.add(str(a))
                    if t.get("isin"):
                        isins.add(str(t["isin"]))
                    if t.get("description"):
                        descs.add(str(t["description"]))
                    if sec == "timeDeposit" and t.get("assetId"):
                        td_ndf.add(f"TD{t.get('assetId')}")
                    if sec == "forwards" and t.get("assetId"):
                        td_ndf.add(f"NDF{t.get('assetId')}")
    accts.discard("")
    return sorted(aids), sorted(isins), sorted(descs), sorted(accts), sorted(td_ndf)

def _inlist(vals):
    return ",".join("'" + str(v).replace("'", "''") + "'" for v in vals) or "''"

def emit_selects(aids, isins, descs, accts, td_ndf, base):
    needed = {"assetIds": aids, "isins": isins, "descriptions": descs, "accounts": accts,
              "td_ndf_canonicals": td_ndf}
    json.dump(needed, open(base + ".lookups_needed.json", "w"), indent=2)
    print(f"Pass 1 - wrote {base}.lookups_needed.json "
          f"({len(aids)} assetIds, {len(isins)} ISINs, {len(accts)} accounts)")
    A, I, D, C, T = _inlist(aids), _inlist(isins), _inlist(descs), _inlist(accts), _inlist(td_ndf)
    print("\n-- q1 assets_custody")
    print(f"SELECT TickerCustody, Asset FROM Portfolio.v_AssetCustody WHERE Custody='{CUSTODY}' AND TickerCustody IN ({A}) AND Asset IS NOT NULL;")
    print("\n-- q2 assets_isin")
    print(f"SELECT Asset, Isin FROM Global.v_Asset WHERE Isin IN ({I}) AND Isin IS NOT NULL;")
    print("\n-- q3 assets_desc")
    print(f"SELECT Asset, Description FROM Global.v_Asset WHERE Description IN ({D});")
    print("\n-- q4 valid_types")
    print("SELECT 'TT' AS K, TransactionType AS V FROM Global.TransactionType UNION ALL SELECT 'GL', GeneralLedgerType FROM Global.GeneralLedgerType;")
    print("\n-- q5 locks")
    print(f"SELECT Account, CONVERT(varchar(10),[Date],120) AS CheckedDate FROM Portfolio.v_CheckedDate WHERE Custody='{CUSTODY}' AND Activated=1 AND Account IN ({C});")
    print("\n-- q6 held_assets")
    print(f"SELECT DISTINCT p.Account, p.Asset, a.Description FROM Portfolio.v_CustodyPosition p JOIN Global.v_Asset a ON a.Asset=p.Asset WHERE p.Custody='{CUSTODY}' AND p.Account IN ({C}) AND p.Asset IS NOT NULL;")
    print("\n-- q7 existing_tx (dedup + expiry net; last ~2y)")
    print(f"SELECT ClientAccount, CONVERT(varchar(10),[Date],120) AS Date, TransactionType, CustodyIdentifier, Value, Asset, Quantity, RawTransaction FROM Portfolio.v_AccountTransaction WHERE Custody='{CUSTODY}' AND ClientAccount IN ({C}) AND CAST([Date] AS date) >= DATEADD(year,-2,CAST(GETDATE() AS date));")
    print("\n-- q8 existing_assets (TD/NDF already registered)")
    print(f"SELECT Asset FROM Global.v_Asset WHERE Asset IN ({T});")
    print("\n-- q9 inception_seed (AccountPosition at active CheckedDate)")
    print(f"SELECT ap.Account, ap.Asset, ap.QuantityClose FROM Portfolio.v_AccountPosition ap JOIN Portfolio.v_CheckedDate cd ON cd.Account=ap.Account AND cd.Custody='{CUSTODY}' AND cd.Activated=1 AND CAST(ap.Date AS date)=CAST(cd.Date AS date) WHERE ap.Account IN ({C});")
    print("\n-- q10 fop_unit_value (only needed if Free-of-Payment transfers present; latest CustodyPosition unit value)")
    print(f"SELECT Asset, Value, Quantity, CONVERT(varchar(10),[Date],120) AS Date FROM Portfolio.v_CustodyPosition WHERE Custody='{CUSTODY}' AND Account IN ({C}) AND Quantity<>0 ORDER BY [Date] DESC;")

def build_lookups(args):
    out = {"assets_custody": {}, "assets_isin": {}, "assets_desc": {}, "valid_tt": [], "valid_gl": [],
           "locks": {}, "held_assets": {}, "existing_assets": [], "existing_tx": [],
           "inception_seed": {}, "fop_unit_value": {}}
    if args.q1:
        for r in load_rows(args.q1):
            out["assets_custody"][str(col(r, "TickerCustody"))] = col(r, "Asset")
    if args.q2:
        for r in load_rows(args.q2):
            if col(r, "Isin"):
                out["assets_isin"][str(col(r, "Isin"))] = col(r, "Asset")
    if args.q3:
        for r in load_rows(args.q3):
            if col(r, "Description"):
                out["assets_desc"][str(col(r, "Description"))] = col(r, "Asset")
    if args.q4:
        for r in load_rows(args.q4):
            k, v = col(r, "K"), col(r, "V")
            if k == "TT" and v:
                out["valid_tt"].append(v)
            elif k == "GL" and v:
                out["valid_gl"].append(v)
    if args.q5:
        for r in load_rows(args.q5):
            out["locks"][str(col(r, "Account"))] = str(col(r, "CheckedDate"))[:10]
    if args.q6:
        for r in load_rows(args.q6):
            out["held_assets"].setdefault(str(col(r, "Account")), []).append(
                [col(r, "Asset"), col(r, "Description")])
    if args.q7:
        out["existing_tx"] = load_rows(args.q7)
    if args.q8:
        for r in load_rows(args.q8):
            if col(r, "Asset"):
                out["existing_assets"].append(col(r, "Asset"))
    if args.q9:
        for r in load_rows(args.q9):
            if col(r, "QuantityClose") is not None:
                out["inception_seed"].setdefault(str(col(r, "Account")), {})[str(col(r, "Asset"))] = _f(col(r, "QuantityClose"))
    if args.q10:
        # keep the most-recent unit value per asset (rows already ordered Date DESC)
        for r in load_rows(args.q10):
            a = str(col(r, "Asset"))
            if a in out["fop_unit_value"]:
                continue
            q = _f(col(r, "Quantity"))
            if q:
                out["fop_unit_value"][a] = abs(_f(col(r, "Value")) / q)
    json.dump(out, open(args.out, "w"), indent=2, default=str)
    print(f"Wrote {args.out}: "
          f"{len(out['assets_custody'])} custody maps, {len(out['assets_isin'])} ISIN maps, "
          f"{len(out['valid_tt'])} TT, {len(out['valid_gl'])} GL, {len(out['locks'])} locks, "
          f"{len(out['existing_tx'])} existing tx, {len(out['existing_assets'])} TD/NDF exist, "
          f"{len(out['inception_seed'])} seeded accts")

# --------------------------------------------------------------------------- review
def print_review(plan_accounts, reg_candidates, reg_maps, L, stats):
    print("=" * 96)
    print(f"BTG OFFSHORE TRANSACTION PLAN   custody={CUSTODY}   (review - nothing written)")
    print("=" * 96)
    new_reg = [c for c in reg_candidates if c not in L.existing_assets]
    print(f"Accounts: {len(plan_accounts)} | TD/NDF to register: {len(new_reg)} | "
          f"option maps: {len(reg_maps)}")
    print(f"Totals -> validated={stats['validated']} pending={stats['pending']} "
          f"lock_blocked={stats['lock_blocked']} dropped_invalid={stats['dropped_invalid']} "
          f"skipped_dup={stats['skipped_dup']}")
    for account, pa in plan_accounts.items():
        print("-" * 96)
        print(f"account {account}  lock={pa['lock'] or '-'}  "
              f"validated={pa['validated']} pending={pa['pending']} "
              f"lock_blocked={pa['lock_blocked']} dropped={pa['dropped_invalid']} dup={pa['skipped_dup']}")
        bybuck = collections.Counter(r["_bucket"] for r in pa["rows"])
        bytype = collections.Counter(r["_params"]["TransactionType"] for r in pa["rows"])
        print("   buckets:", dict(bybuck), "| types:", dict(bytype))
        for r in pa["rows"]:
            p = r["_params"]
            print(f"   {p['Date']} {p['TransactionType']:<22} {p.get('GeneralLedgerType','') or '':<18} "
                  f"{(p['Asset'] or '-'):<14} qty={p['Quantity']:<14.4f} val={p['Value']:<16.2f} "
                  f"{p['Status']:<10} {p['Obs'][:42]}")

# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("raw", nargs="+")
    ap.add_argument("--build-lookups", action="store_true")
    ap.add_argument("--lookups")
    ap.add_argument("--batch")
    ap.add_argument("--accounts")
    for q in range(1, 11):
        ap.add_argument(f"--q{q}")
    ap.add_argument("--out", default="shared.lookups.json")
    args = ap.parse_args()

    datasets = [load_raw(p) for p in args.raw]
    base = args.raw[0]

    if args.build_lookups:
        build_lookups(args)
        return

    if not args.lookups:
        aids, isins, descs, accts, td_ndf = discover(datasets)
        emit_selects(aids, isins, descs, accts, td_ndf, base)
        return

    L = Lookups(json.loads(open(args.lookups, encoding="utf-8").read()))
    accounts_filter = set(a.strip() for a in args.accounts.split(",")) if args.accounts else None
    # merge all datasets into one trades list for the plan
    merged = {"trades": []}
    for d in datasets:
        merged["trades"] += d.get("trades", [])
    plan_accounts, reg_candidates, reg_maps, stats = build_plan(merged, L, accounts_filter)
    print_review(plan_accounts, reg_candidates, reg_maps, L, stats)

    plan_out = {"custody": CUSTODY,
                "accounts": {a: {k: v for k, v in pa.items() if k != "rows"} |
                                {"rows": [r["_params"] for r in pa["rows"]],
                                 "buckets": dict(collections.Counter(r["_bucket"] for r in pa["rows"]))}
                             for a, pa in plan_accounts.items()},
                "registrations": [c for c in reg_candidates if c not in L.existing_assets],
                "option_maps": [m["ticker"] for m in reg_maps if m["ticker"] not in L.assets_custody],
                "totals": dict(stats)}
    json.dump(plan_out, open(base + ".plan.json", "w"), indent=2, default=str)
    print(f"\nPlan written to {base}.plan.json")

    if args.batch:
        os.makedirs(args.batch, exist_ok=True)
        manifest = []
        first = True
        for account, pa in plan_accounts.items():
            items = batch_items_for_account(account, pa, reg_candidates, reg_maps, L, first)
            first = False
            fn = os.path.join(args.batch, f"items_{account}.json")
            json.dump(items, open(fn, "w"), indent=2, default=str)
            manifest.append({"account": account, "items_file": fn, "n_items": len(items),
                             "validated": pa["validated"], "pending": pa["pending"]})
        json.dump({"custody": CUSTODY, "batches": manifest},
                  open(os.path.join(args.batch, "manifest.json"), "w"), indent=2)
        print(f"Batch items written to {args.batch}/ ({len(manifest)} account batch(es))")

if __name__ == "__main__":
    main()
