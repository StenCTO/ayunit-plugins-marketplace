# asset

Security-master tools for the Sten book of record. Centred on two tables
accessed live through the ayunit MCP:

| Table / view | Role |
|---|---|
| `Global.Asset` / `Global.v_Asset` | **Security master.** One row per financial instrument, keyed by `pk_AssetID` with the `Asset` code as the natural (UNIQUE) key. Almost every position, transaction, and price points back to it via `fk_AssetID`. |
| `Portfolio.AssetCustody` / `Portfolio.v_AssetCustody` | **Custody ↔ asset mapping.** Per-custodian identifier (CUSIP, ticker, internal ID) ↔ `Global.Asset` link, with scaling factors (`PositionFactor`, `PriceFactor`). Required for any custody feed to resolve into a position. |

The plugin is a **container**: one skill per workflow (register a new asset,
map a missing custody identifier, soft-delete, audit a classification, …).
Each skill keeps the same contract as `account-transaction` and `position`:
parser/orchestrator runs locally, **all DB access goes through the ayunit
MCP**, writes are explicit and verified.

## Skills

| Skill | Scope | What it does |
|---|---|---|
| `asset-register` | `Global.Asset` `I` | Registers a NEW instrument in the security master by **analogy with existing peers** — queries assets of the same kind already in the book and copies their classification convention, validates every lookup string (`AssetGroup`, `SecurityType`, `Product`, `AssetClass`, `Benchmark`, `Source`, `TaxRegime`), runs the duplicate + identifier gate, previews the full row, and INSERTs via `Global.Asset_Update @CMD='I'`, then verifies every FK resolved. |

## Requirements

- **The ayunit MCP must be connected** in the session. Every read goes through
  `execute_select_query`; every write through the appropriate `*_Update`
  procedure (`Global.Asset_Update`, `Portfolio.AssetCustody_Update`, …) —
  never direct DML. The skills never hold credentials and never hit the DB
  any other way.
- Reference docs live on the same MCP — `search_docs` / `read_doc` for
  `asset/faq`, `asset/relationship`, `asset/procedure`, and any related pages.

## Design contract (shared by every skill here)

- **Single source of truth = the live DB via the ayunit MCP.** No hard-coded
  classification taxonomies, identifier maps, or peer lists — all read live.
- **Registration by analogy, not by guessing.** Every classification value
  comes from (and matches) an existing peer's convention and must already
  exist in its lookup table. Never invent new `AssetGroup`/`AssetClass`/
  `Benchmark`/`Source`/`Issuer` values on your own — that's a deliberate
  taxonomy decision for the user.
- **Two independent hierarchies.** Operational (`AssetGroup → SecurityType`)
  drives pricing routines; strategy/reporting (`Product → AssetClass`) drives
  PnL-by-strategy. Never cross them — `Renda Fixa` is a `Product`, never an
  `AssetGroup`.
- **Preview-and-confirm before every write; verify after.** No insert on
  implied approval. Production is always one explicit yes away.
- **Reply in the user's language** (PT/EN) and echo the resolved scope
  (proposed code + normalised identifiers) at the top of every reply.

## Adding a new skill

1. Create `skills/<skill-name>/` (kebab-case).
2. Write `SKILL.md` with frontmatter (`name`, `description` — the
   description's `Use when …` triggers are what fire the skill in chat).
3. If the skill scripts anything (parser, mapper), drop the script alongside
   `SKILL.md` and keep DB access strictly through the MCP.
4. Bump this plugin's `version` in `.claude-plugin/plugin.json` and push to
   both remotes (Azure + GitHub) so teammates pick it up via
   `/plugin marketplace update sten-ayunit`.

---
_Sten Capital · v0.1.0 (draft)_
