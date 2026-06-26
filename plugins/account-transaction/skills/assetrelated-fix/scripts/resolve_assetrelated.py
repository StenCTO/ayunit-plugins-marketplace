#!/usr/bin/env python3
"""
resolve_assetrelated.py  —  AssetRelated resolver for GL-RECEIPT income rows.

Deterministic matching engine for the `assetrelated-fix` skill. It reads the
defect candidates (GENERAL LEDGER RECEIPT / INTEREST-DIVIDEND rows whose
AssetRelated is unresolved) and the account's *holding universe* (assets the
account holds or has ever traded), parses the originating security out of the
custody description, confirms it against the holding universe, and emits a
per-row verdict with a conviction tier.

It NEVER touches the database. It only turns text + holdings into a plan; the
skill (the agent) does the lock-gating and the writes.

Usage
-----
    python resolve_assetrelated.py candidates.json holdings.json > plan.json

candidates.json : list of objects, one per defect row, with at least:
    pk            (int)      pk_AccountTransactionID
    description   (str)      COALESCE(GeneralLedgerDescription, Obs)
    status        (str)      PENDING | VALIDATED | UPDATED
  optional, echoed back untouched:
    date, settlementDate, clientAccount, custody, value

holdings.json   : list of objects, one per asset the account holds/traded:
    asset         (str)      the Asset code used in the book (what AssetRelated must become)
    description   (str)      Global.v_Asset.Description (human name, for fuzzy match)

plan.json (stdout) : the candidates, each enriched with:
    layout        'A' | 'B' | 'UNKNOWN'
    extracted     the raw token/name pulled from the description (or null)
    matchedAsset  the Asset code to write into AssetRelated (or null)
    matchedName   the matched holding's Description (or null)
    score         fuzzy score 0..1 of the chosen match (1.0 for an exact ticker hit)
    conviction    'HIGH' | 'REPORT'
    reason        short human explanation

Conviction policy
-----------------
HIGH (the skill may auto-fix, subject to the CheckedDate lock):
  * Layout A: the ticker parsed from the text equals — exactly — the Asset code
    (or ticker) of one, and only one, asset in the holding universe.
  * Layout B: the fund name parsed from the text fuzzy-matches exactly one
    holding with score >= HIGH_THRESHOLD and a clear margin over the runner-up.
REPORT (never auto-written — listed for a human):
  * no parseable layout, OR
  * the parsed security is not in the holding universe (coherence fails), OR
  * the match is ambiguous (no clear unique winner).

The whole point of the holding universe is coherence: AssetRelated is only ever
set to a security the account actually holds. A name in the text that the
account does not hold is reported, never guessed.
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


# ---- Layout parsers --------------------------------------------------------

# A: "RENDIMENTOS DE CLIENTES <TICKER> S/ <n>"  -> the ticker sits between
#    'CLIENTES' and the ' S/' quantity marker.
RE_A = re.compile(r"RENDIMENTOS\s+DE\s+CLIENTES\s+(.+?)\s+S/", re.IGNORECASE)

# B: "...Tx de Distr <FUND NAME>" / "...Distribuição <FUND NAME>"  -> everything
#    after the 'Distr...' word is the fund name.
RE_B = re.compile(r"DISTR\w*\s+(.+)$", re.IGNORECASE)


def parse_layout(description: str):
    """Return (layout, extracted_text) or ('UNKNOWN', None)."""
    d = description or ""
    m = RE_A.search(d)
    if m:
        return "A", m.group(1).strip()
    if re.search(r"DEVOLU\w*", deaccent(d)) and RE_B.search(d):
        return "B", RE_B.search(d).group(1).strip()
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
    if not cand:
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


# ---- Driver ----------------------------------------------------------------

def resolve(candidates, holdings):
    out = []
    for c in candidates:
        row = dict(c)
        layout, extracted = parse_layout(c.get("description", ""))
        row["layout"] = layout
        row["extracted"] = extracted
        row["matchedAsset"] = None
        row["matchedName"] = None
        row["score"] = None
        row["conviction"] = "REPORT"
        row["reason"] = ""

        if layout == "UNKNOWN":
            row["reason"] = "description matches no known income layout (A/B) - manual"
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

        # layout == "B"
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
    return out


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: resolve_assetrelated.py candidates.json holdings.json > plan.json")
    with open(sys.argv[1], encoding="utf-8") as f:
        candidates = json.load(f)
    with open(sys.argv[2], encoding="utf-8") as f:
        holdings = json.load(f)
    plan = resolve(candidates, holdings)
    # Summary to stderr so stdout stays clean JSON.
    n_high = sum(1 for r in plan if r["conviction"] == "HIGH")
    print(f"[resolve] {len(plan)} candidates -> {n_high} HIGH, {len(plan)-n_high} REPORT",
          file=sys.stderr)
    json.dump(plan, sys.stdout, ensure_ascii=False, indent=2)
    print()


if __name__ == "__main__":
    main()
