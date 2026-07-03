#!/usr/bin/env python3
"""
resolve_assetrelated.py  —  AssetRelated resolver for GL-RECEIPT income rows.

Deterministic matching engine for the `assetrelated-fix` skill. It reads the
defect candidates (GENERAL LEDGER RECEIPT / INTEREST-DIVIDEND rows whose
AssetRelated is unresolved), the account's *holding universe* (for Layouts
A/B), and a *global identifier index* (for Layout C), parses the originating
security out of the custody description, confirms it, and emits a per-row
verdict with a conviction tier.

It NEVER touches the database. It only turns text + reference data into a
plan; the skill (the agent) does the lock-gating and the writes.

Usage
-----
    python resolve_assetrelated.py candidates.json holdings.json [identifiers.json] > plan.json

candidates.json : list of objects, one per defect row, with at least:
    pk            (int)      pk_AccountTransactionID
    description   (str)      COALESCE(GeneralLedgerDescription, Obs)
    status        (str)      PENDING | VALIDATED | UPDATED
  optional, echoed back untouched:
    date, settlementDate, clientAccount, custody, value
  optional, used ONLY by Layout C:
    cusip         (str)      the custody-side CUSIP for this income event, when the
                              feed carries it as a separate field rather than embedded
                              in the description (this is JP's case — RawTransaction.Cusip
                              is always populated, but the free-text Description only
                              *sometimes* also embeds an ISIN). Pull it from RawTransaction
                              before calling this script; the resolver does not parse JSON.

holdings.json   : list of objects, one per asset the account holds/traded (Layouts A/B only):
    asset         (str)      the Asset code used in the book (what AssetRelated must become)
    description   (str)      Global.v_Asset.Description (human name, for fuzzy match)

identifiers.json : a GLOBAL (not per-account) identifier index, one object per asset
  that has a known ISIN/CUSIP anywhere in the book:
    asset         (str)      the Asset code
    isin          (str|null) Global.Asset.Isin
    cusip         (str|null) Global.Asset.Cusip
    custodyTicker (str|null) Portfolio.AssetCustody.TickerCustody  for the row's Custody
    custodyTicker2(str|null) Portfolio.AssetCustody.TickerCustody2 for the row's Custody
  Build it with (swap Custody for the feed you're resolving — JP shown):
    SELECT a.Asset, a.Isin, a.Cusip,
           ac.TickerCustody  AS custodyTicker,
           ac.TickerCustody2 AS custodyTicker2
    FROM Global.v_Asset a
    LEFT JOIN Portfolio.v_AssetCustody ac ON ac.Asset = a.Asset AND ac.Custody = 'JP'
    WHERE a.Isin IS NOT NULL OR a.Cusip IS NOT NULL
       OR ac.TickerCustody IS NOT NULL OR ac.TickerCustody2 IS NOT NULL;
  Pull it fresh each run (it's reference data, cheap to query) — don't cache it stale.

plan.json (stdout) : the candidates, each enriched with:
    layout        'A' | 'B' | 'C' | 'SWEEP' | 'UNKNOWN'
    extracted     the raw token/name pulled from the description (or null)
    matchedAsset  the Asset code to write into AssetRelated (or null)
    matchedName   the matched holding's Description (or null)
    score         fuzzy score 0..1 of the chosen match (1.0 for an exact hit)
    conviction    'HIGH' | 'REPORT'
    reason        short human explanation

Conviction policy
-----------------
HIGH (the skill may auto-fix, subject to the CheckedDate lock):
  * Layout A: the ticker parsed from the text equals — exactly — the Asset code
    (or ticker) of one, and only one, asset in the holding universe.
  * Layout B: the fund name parsed from the text fuzzy-matches exactly one
    holding with score >= HIGH_THRESHOLD and a clear margin over the runner-up.
  * Layout C: the ISIN parsed from the description, OR the cusip field passed
    alongside the candidate, matches — EXACTLY — one and only one asset in the
    identifiers.json index (Global.Asset.Isin/Cusip or the custody-side
    TickerCustody/TickerCustody2 for that feed). Unlike A/B, Layout C does NOT
    require the account to already hold the asset. Rationale: an ISIN or CUSIP
    is a globally unique identifier — a match is proof of identity on its own,
    no fuzziness involved. Requiring a prior "held" row as well would silently
    block legitimate coupon income whenever the position feed hasn't caught up
    with (or doesn't cover the same scope as) the income feed, which is what
    was observed on real JP bond coupons whose AssetCustody/Isin resolved
    cleanly but had zero rows in AccountPosition / AccountTransaction /
    CustodyPosition for that account.
REPORT (never auto-written — listed for a human):
  * no parseable layout, OR
  * (A/B only) the parsed security is not in the holding universe (coherence
    fails), OR
  * (C) the ISIN/CUSIP does not match any asset in identifiers.json (the
    security is genuinely unregistered — needs asset-register first), OR
  * (SWEEP) cash-sweep interest on the USD balance itself — no paying
    security, and the row is loader-mis-classified as
    GeneralLedgerType='INTEREST/DIVIDEND' when the correct type is
    'OVERNIGHT' with AssetRelated NULL by design. The reclassification is
    NOT this skill's job (this skill writes only AssetRelated + Status);
    report and hand off to the loader / GL-type fix, OR
  * the match is ambiguous (no clear unique winner, or the same identifier
    resolves to >1 asset — a data problem in Global.Asset that should be
    fixed there, not papered over here).

The coherence rule for A/B remains: AssetRelated is only ever set to a security
the account actually holds. A name in the text that the account does not hold
is reported, never guessed. Layout C bypasses coherence because ISIN/CUSIP is
identity, not a hint.
"""

import json
import re
import sys
import unicodedata
from difflib import SequenceMatcher

# Fuzzy thresholds for Layout B (fund-name matching).
HIGH_THRESHOLD = 0.86   # best score must reach this to qualify as HIGH
MARGIN = 0.12           # best must beat the runner-up by at least this much

# Tokens that describe fund *structure*, not *identity*. Stripped before
# fuzzy matching so the distinctive name dominates the score.
STOPWORDS = {
    "FIC", "FICFIM", "FICFI", "FI", "FIF", "FIM", "FIA", "FIRF", "FIDC", "FII",
    "FUNDO", "FUNDOS", "FDO", "FD",
    "INVESTIMENTO", "INVESTIMENTOS", "INVEST", "INVESTMENT",
    "MULTIMERCADO", "MM", "RENDA", "FIXA", "RF", "ACOES", "ACAO",
    "CREDITO", "CRPR", "CRED", "PRIVADO", "PRIV", "CP",
    "REFERENCIADO", "REF", "DI", "RL", "LP", "CDB",
    "ADVISORY", "ADV", "MASTER", "FEEDER", "CLASSE", "CLASS", "COTAS", "COTA",
    "DE", "DA", "DO", "DAS", "DOS", "E", "EM", "REAIS", "BRL",
}


def deaccent(s: str) -> str:
    """Lowercase-fold accents away and uppercase the result."""
    nfkd = unicodedata.normalize("NFKD", s or "")
    no_marks = "".join(c for c in nfkd if not unicodedata.combining(c))
    return no_marks.upper()


def tokens(s: str):
    """Alphanumeric tokens of a de-accented string."""
    return [t for t in re.split(r"[^A-Z0-9]+", deaccent(s)) if t]


def signature(name: str) -> str:
    """
    Collapse a fund name to its distinctive signature: drop structure stopwords,
    concatenate the rest (no spaces). 'SUL AMERICA EXCLUSIVE FIRF DI' and
    'SulAmérica Exclusive FIRF REF DI' both collapse to 'SULAMERICAEXCLUSIVE',
    so feed quirks (spacing, accents, word order of structure words) wash out.
    """
    distinctive = [t for t in tokens(name) if t not in STOPWORDS]
    return "".join(distinctive)


# ---- Curated alias map (human-confirmed name -> Asset overrides) -------------

import os
_ALIAS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alias_map.json")

def _alias_norm(name):
    s = deaccent(name or "").strip()
    s = re.sub(r"^-\s*", "", s)
    return re.sub(r"\s+", " ", s)

def _load_aliases():
    by_key, by_sig = {}, {}
    try:
        with open(_ALIAS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        for a in data.get("aliases", []):
            asset = a.get("asset")
            if not asset:
                continue
            by_key[_alias_norm(a.get("name", ""))] = asset
            sg = signature(a.get("name", ""))
            if sg:
                by_sig.setdefault(sg, asset)
    except FileNotFoundError:
        pass
    return by_key, by_sig

_ALIAS_BY_KEY, _ALIAS_BY_SIG = _load_aliases()

def alias_lookup(name):
    """Return the human-confirmed Asset for a parsed name, or None."""
    if not name:
        return None
    k = _alias_norm(name)
    if k in _ALIAS_BY_KEY:
        return _ALIAS_BY_KEY[k]
    return _ALIAS_BY_SIG.get(signature(name))


# ---- Layout parsers --------------------------------------------------------

# A: "RENDIMENTOS DE CLIENTES <TICKER> S/ <n>"  -> the ticker sits between
#    'CLIENTES' and the ' S/' quantity marker.
RE_A = re.compile(r"RENDIMENTOS\s+DE\s+CLIENTES\s+(.+?)\s+S/", re.IGNORECASE)

# B: "...Tx de Distr <FUND NAME>" / "...Distribuição <FUND NAME>"  -> everything
#    after the 'Distr...' word is the fund name.
RE_B = re.compile(r"DISTR\w*\s+(.+)$", re.IGNORECASE)

# C: a bare ISIN token anywhere in the description. ISO 6166: 2 letters
#    (country) + 9 alphanumeric (NSIN) + 1 numeric check digit = 12 chars.
#    JP's Description field embeds this for *some* but not all bond coupons
#    ("...FLOATING RATE NOTE XS2529543766 AS OF..."); when it's absent, Layout
#    C falls back to the candidate's separately-supplied `cusip` field instead
#    — so RE_C matching nothing is not itself a failure, only "no ISIN in the
#    text AND no cusip field" is.
RE_C_ISIN = re.compile(r"\b([A-Z]{2}[A-Z0-9]{9}[0-9])\b")

# SWEEP: JP's monthly cash-sweep interest — "DEPOSIT SWEEP INTEREST FOR
# <period> @ <rate> RATE ON AVG COLLECTED BALANCE OF $<amt> AS OF <date>".
# This is interest earned on the USD cash balance itself, NOT a coupon on a
# security — there is no paying asset. The house rule (confirmed 2026-07-03)
# is that the loader misclassifies these rows as GeneralLedgerType =
# 'INTEREST/DIVIDEND' when the correct classification is
# GeneralLedgerType = 'OVERNIGHT' with AssetRelated = NULL. Both fixes are
# out of THIS skill's write scope (the skill only writes AssetRelated +
# Status). So the resolver recognises them as a distinct layout and
# REPORTs each with an explicit "reclassify at loader / GL type" reason,
# instead of dumping them into the generic UNKNOWN bucket where a real
# unknown grammar might hide.
RE_SWEEP = re.compile(r"DEPOSIT\s+SWEEP\s+INTEREST", re.IGNORECASE)


def parse_layout(description: str, has_cusip: bool = False):
    """Return (layout, extracted_text) or ('UNKNOWN', None).

    extracted_text for layout 'C' is the ISIN found in the text, or None if
    only the out-of-band cusip field is available (resolve() checks that
    field directly via match_identifier). For 'SWEEP', extracted_text is
    None — recognition alone is the whole point of the layout.

    Precedence: A > B > SWEEP > C. SWEEP is checked before C because a
    sweep-interest description never carries an ISIN or a bond CUSIP — its
    'cusip' field, if any, is spurious — and we don't want a rare stray
    12-char token in the sweep template to accidentally route it to C.
    """
    d = description or ""
    m = RE_A.search(d)
    if m:
        return "A", m.group(1).strip()
    if re.search(r"DEVOLU\w*", deaccent(d)) and RE_B.search(d):
        return "B", RE_B.search(d).group(1).strip()
    if RE_SWEEP.search(d):
        return "SWEEP", None
    m = RE_C_ISIN.search(deaccent(d))
    if m:
        return "C", m.group(1)
    if has_cusip:
        # No ISIN in the free text, but the caller supplied a cusip out of
        # band (JP's Cusip field, present on every row even when the
        # Description doesn't happen to embed an ISIN). Still Layout C.
        return "C", None
    return "UNKNOWN", None


# ---- Matchers --------------------------------------------------------------

def match_ticker(ticker: str, holdings):
    """
    Layout A: exact ticker -> holding. Match the parsed token against each
    holding's Asset code (and the first token of its Description, to catch
    books that store the ticker in the name). Unique exact hit -> HIGH.
    """
    tnorm = deaccent(ticker).strip()
    hits = []
    for h in holdings:
        acode = deaccent(h.get("asset", "")).strip()
        dtok = tokens(h.get("description", ""))
        if tnorm and (tnorm == acode or (dtok and tnorm == dtok[0])):
            hits.append(h)
    if len(hits) == 1:
        return hits[0], 1.0
    return None, 0.0


def match_name(name: str, holdings):
    """
    Layout B: fuzzy fund-name -> holding via signature similarity.
    Returns (best_holding, best_score, unique_clear_winner: bool).
    """
    cand = signature(name)
    if not cand or not holdings:
        return None, 0.0, False
    scored = []
    for h in holdings:
        hs = signature(h.get("description", "")) or "".join(tokens(h.get("asset", "")))
        score = SequenceMatcher(None, cand, hs).ratio() if hs else 0.0
        scored.append((score, h))
    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best = scored[0]
    runner = scored[1][0] if len(scored) > 1 else 0.0
    unique = best_score >= HIGH_THRESHOLD and (best_score - runner) >= MARGIN
    return best, best_score, unique


def match_identifier(isin, cusip, identifiers):
    """
    Layout C: exact ISIN or CUSIP -> asset, against the GLOBAL identifier index
    (not per-account holdings — see module docstring for why). Checks
    Global.Asset.Isin/Cusip AND the custody-side TickerCustody/TickerCustody2
    (JP, and other custodies, sometimes book a different CUSIP variant on their
    side than the one stored on Global.Asset — confirmed on BNP Barclays PLC
    CLN: Global.Asset.Cusip='ZJ3714574' but JP's own feed CUSIP is '6M023A9D0',
    which only resolves via the Portfolio.AssetCustody mapping).

    A row identifies uniquely if exactly one asset in the index matches on any
    of: Isin, Cusip, custodyTicker, custodyTicker2. If ISIN and CUSIP are both
    present and they disagree (point to two different assets), that's a data
    conflict -> REPORT, not a guess.
    """
    isin_n = deaccent(isin).strip() if isin else None
    cusip_n = deaccent(cusip).strip() if cusip else None
    if not isin_n and not cusip_n:
        return None, "no ISIN in text and no cusip field supplied"

    def hits_for(key, val):
        if not val:
            return []
        return [h for h in identifiers if deaccent(h.get(key, "") or "") == val]

    isin_hits = {h["asset"] for h in hits_for("isin", isin_n)} if isin_n else set()
    cusip_hits = set()
    if cusip_n:
        for key in ("cusip", "custodyTicker", "custodyTicker2"):
            cusip_hits |= {h["asset"] for h in hits_for(key, cusip_n)}

    all_hits = isin_hits | cusip_hits
    if isin_n and cusip_n and isin_hits and cusip_hits and isin_hits != cusip_hits:
        return None, (f"ISIN '{isin}' and CUSIP '{cusip}' resolve to different "
                      f"assets ({sorted(isin_hits)} vs {sorted(cusip_hits)}) - conflict, manual")
    if len(all_hits) == 1:
        return next(iter(all_hits)), None
    if not all_hits:
        return None, (f"ISIN/CUSIP not found in Global.Asset or Portfolio.AssetCustody "
                      f"(isin={isin!r}, cusip={cusip!r}) - not registered, manual")
    return None, f"identifier matched >1 asset ({sorted(all_hits)}) - ambiguous, manual"


# ---- Driver ----------------------------------------------------------------

def resolve(candidates, holdings, identifiers=None):
    identifiers = identifiers or []
    out = []
    for c in candidates:
        row = dict(c)
        has_cusip = bool(c.get("cusip"))
        layout, extracted = parse_layout(c.get("description", ""), has_cusip=has_cusip)
        row["layout"] = layout
        row["extracted"] = extracted
        row["matchedAsset"] = None
        row["matchedName"] = None
        row["score"] = None
        row["conviction"] = "REPORT"
        row["reason"] = ""

        # --- curated alias map (human-confirmed) takes precedence over fuzzy/coherence ---
        # Restricted to A/B: for Layout C, `extracted` is an ISIN, not a fund name.
        ahit = alias_lookup(extracted) if layout in ("A", "B") else None
        if ahit:
            row.update(matchedAsset=ahit, score=1.0, conviction="HIGH",
                       reason=f"alias map (human-confirmed): '{extracted}' -> {ahit}")
            out.append(row)
            continue

        if layout == "SWEEP":
            row["reason"] = ("cash-sweep interest on the USD balance itself (no paying "
                             "security); loader mis-classifies as GeneralLedgerType "
                             "'INTEREST/DIVIDEND' - correct type is 'OVERNIGHT' with "
                             "AssetRelated NULL by design. Reclassify at loader / GL type "
                             "(out of this skill's write scope, which only sets "
                             "AssetRelated + Status)")
            out.append(row)
            continue

        if layout == "UNKNOWN":
            row["reason"] = "description matches no known income layout (A/B/C/SWEEP) - manual"
            out.append(row)
            continue

        if layout == "A":
            hit, score = match_ticker(extracted, holdings)
            if hit:
                row.update(matchedAsset=hit["asset"], matchedName=hit.get("description"),
                           score=round(score, 4), conviction="HIGH",
                           reason=f"ticker '{extracted}' == held asset {hit['asset']} (exact, unique)")
            else:
                row["reason"] = (f"ticker '{extracted}' not in account holding universe "
                                 f"(or matched >1) - coherence fails, manual")
            out.append(row)
            continue

        if layout == "B":
            best, score, unique = match_name(extracted, holdings)
            if best and unique:
                row.update(matchedAsset=best["asset"], matchedName=best.get("description"),
                           score=round(score, 4), conviction="HIGH",
                           reason=f"fund '{extracted}' ~ held {best['asset']} "
                                  f"({best.get('description')}) score={score:.2f}, unique")
            else:
                bn = best.get("description") if best else None
                row["score"] = round(score, 4) if best else None
                row["reason"] = (f"fund '{extracted}' best holding '{bn}' score={score:.2f} "
                                 f"below threshold/ambiguous - manual")
            out.append(row)
            continue

        # layout == "C"
        isin = extracted  # None if only the out-of-band cusip field applies
        cusip = c.get("cusip")
        asset, fail_reason = match_identifier(isin, cusip, identifiers)
        if asset:
            evidence = []
            if isin:
                evidence.append(f"ISIN {isin}")
            if cusip:
                evidence.append(f"CUSIP {cusip}")
            row.update(matchedAsset=asset, score=1.0, conviction="HIGH",
                       reason=f"{' + '.join(evidence)} == registered asset {asset} "
                              f"(exact, unique; identifier index, no holding required)")
        else:
            row["reason"] = fail_reason
        out.append(row)
    return out


def main():
    if len(sys.argv) not in (3, 4):
        sys.exit("usage: resolve_assetrelated.py candidates.json holdings.json [identifiers.json] > plan.json")
    with open(sys.argv[1], encoding="utf-8") as f:
        candidates = json.load(f)
    with open(sys.argv[2], encoding="utf-8") as f:
        holdings = json.load(f)
    identifiers = []
    if len(sys.argv) == 4:
        with open(sys.argv[3], encoding="utf-8") as f:
            identifiers = json.load(f)
    plan = resolve(candidates, holdings, identifiers)
    # Summary to stderr so stdout stays clean JSON.
    n_high = sum(1 for r in plan if r["conviction"] == "HIGH")
    by_layout = {}
    for r in plan:
        by_layout[r["layout"]] = by_layout.get(r["layout"], 0) + 1
    print(f"[resolve] {len(plan)} candidates -> {n_high} HIGH, {len(plan)-n_high} REPORT "
          f"(by layout: {by_layout})", file=sys.stderr)
    json.dump(plan, sys.stdout, ensure_ascii=False, indent=2)
    print()


if __name__ == "__main__":
    main()
