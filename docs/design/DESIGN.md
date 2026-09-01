# Traido Design System

**Status:** Locked from user references (Stage 0)  
**References:** [`references/`](references/) — Cabin brand board + MedSync dashboard soft UI  
**Tone:** Calm, premium, warm-minimal — capital seriousness without neon “trader terminal” clichés

---

## Brand

| Item | Value |
|------|--------|
| Name | **Traido** |
| Line | Analyze. Decide. Trade. |
| Voice | Direct, numeric, no hype |
| Visual cousins | Cabin palette + MedSync layout language |

---

## Locked palette (from Cabin board)

| Swatch | Hex | Role in Traido |
|--------|-----|----------------|
| Accent mustard | `#FFCF88` | Primary CTA, opportunity highlight, active accents, PAPER chip, trend pills |
| Taupe | `#B5A18B` | Secondary chart bars, muted chrome, secondary labels |
| Canvas | `#E4E0E0` | App background |
| Ink charcoal | `#201F1E` | Primary text, icons, high-contrast blocks (risk / SELL / schedule-style) |

Supporting surfaces (derived for MedSync-like layering):

| Token | Hex | Use |
|-------|-----|-----|
| `--td-bg-warm` | `#F3EFE8` | Alternate warm canvas / section wash |
| `--td-surface` | `#FFFFFF` | Floating cards |
| `--td-surface-2` | `#F7F4EF` | Nested / inset panels |
| `--td-bull` | `#3D8F6A` | +P&L (desaturated sage) |
| `--td-bear` | `#C45C4A` | −P&L / danger (muted terracotta) |

CSS source of truth: [`tokens.css`](tokens.css)

---

## Visual principles (from references)

1. **Soft UI, not dark terminal** — cream/grey canvas, white floating cards, soft diffuse shadows.
2. **Large corner radii** — cards ~24px, controls often pill (`999px`).
3. **Thin-line icons** — sidebar + header; active nav = white/surface pill with soft shadow.
4. **Big numbers first** — bold metric, quiet label, small mustard trend pill (`↑ 5%`).
5. **Accent sparingly** — mustard for action / highlight blocks; charcoal for solemn blocks (risk, sell).
6. **Charts** — rounded-top bars in taupe; smooth area charts with mustard gradient fill.
7. **Generous whitespace** — airy padding; reduce cognitive load on money decisions.
8. **PAPER banner always visible in V1** — mustard chip in chrome.

---

## Layout mapping (MedSync → Traido)

| MedSync | Traido |
|---------|--------|
| Sidebar (Dashboard, Statistics, …) | Desk, Opportunities, Positions, Agents, Journal, Settings |
| Statistics metric tiles | Equity, Today P&L, Open positions, Win rate |
| Diagnoses bar chart | Score breakdown / volume / setup distribution |
| New patients area chart | Equity curve / portfolio |
| Patients list | Agents status or watchlist |
| Schedule (yellow / charcoal blocks) | Opportunity queue + risk alerts / exit proposals |
| Search + profile pill | Symbol search + account (PAPER) |

### Desk wireframe

```
┌──────┬─────────────────────────────────────────────┬──────────────┐
│ nav  │  search (pill)              PAPER · profile │              │
│ pill │─────────────────────────────────────────────│ Opportunities│
│      │  Equity   Today    Positions   Win rate     │ (mustard /   │
│      │  $100k    +0.41%      4         54.8%       │  ink blocks) │
│      │─────────────────────────────────────────────│              │
│      │  Equity curve (area)   │  Scores (bars)     │ Activity     │
│      │                        │                    │              │
│      │  Agents / Positions list                    │              │
└──────┴─────────────────────────────────────────────┴──────────────┘
```

### Opportunity (Confirm)

- Card: white, radius 24, soft shadow  
- Header block optional mustard for high-confidence BUY  
- Actions: primary mustard **BUY** · ink **DETAILS** · ghost **SKIP**

### Exit

- Ink or muted card for seriousness  
- Actions: ink **SELL** · ghost **HOLD**

---

## Typography

| Role | Family | Notes |
|------|--------|-------|
| UI | **Plus Jakarta Sans** (fallback Manrope) | Geometric soft UI; avoid Inter as brand |
| Figures | **IBM Plex Mono** | Prices, %, R:R, scores |
| Weights | 400 labels · 600 titles · 700 metrics | Large metrics ~32–40px |

---

## Components checklist (Stage 6)

- [x] Soft canvas + white cards (`--td-shadow-md`)
- [x] Sidebar with pill active state
- [x] Pill search field
- [x] Stat tiles + mustard trend pills
- [x] Rounded bar chart (taupe)
- [x] Area chart (mustard gradient)
- [x] Accent / ink schedule-style opportunity blocks
- [x] Confirm actions: mustard / ink / ghost
- [x] PAPER chip always in header

---

## Motion

Sparse, calm:
1. Card enter — short fade  
2. Metric tick — tabular nums only  
3. Agent working — subtle status, no glow spam  

---

## Anti-patterns (do not)

- Dark neon terminal as default theme  
- Purple AI gradients  
- Sharp 4px cards / harsh borders  
- Rainbow chart series  
- Gamified confetti / meme copy  

---

## Engineering handoff

| Artifact | Path |
|----------|------|
| Tokens | `docs/design/tokens.css` |
| References | `docs/design/references/*` |
| Architecture IA | `docs/architecture.md` §12 |
