---
name: impeccable
description: Structured UI design-director toolkit and deterministic anti-slop detector. Provides opinionated passes (critique, audit, polish, distill, typeset, colorize, arrange, bolder, quieter, harden) to eliminate generic AI templates and deliver bespoke, studio-grade interfaces.
source: https://github.com/pbakaus/impeccable
category: ui-design
license: MIT (adapted for WSA)
---

# Impeccable — Design Director & Deterministic Anti-Slop System

Inspired by `pbakaus/impeccable`. Replaces vague "make it look nice" defaults with concrete design-director passes and a strict anti-slop checklist.

## Design-Director Vocabulary (Pick Relevant Passes)

- **Critique**: Judge hierarchy, focal point, visual rhythm, and brand distinctiveness as a senior design director before shipping.
- **Distill**: Strip redundant containers, nested cards-inside-cards, decorative badges, and filler copy until every pixel earns its place.
- **Typeset**: Pair an intentional display face with a readable body face (never default Inter/Roboto everywhere), set tight display tracking (`-0.02em` to `-0.04em`), generous body leading (`1.5–1.65`), max `65–75ch` measure, and `tabular-nums` for metrics/prices.
- **Colorize (Human-Loved Obsidian-Black & Crisp White Baseline for Normal Websites)**: Establish a disciplined token palette (`--bg`, `--surface`, `--surface-2`, `--border`, `--text`, `--muted`, `--accent`). For normal (non-3D) websites, landing pages, SaaS apps, portfolios, and dashboards (unless a light theme is requested), default to a studio-grade **Obsidian-Black & Crisp Alabaster-White** hierarchy (Linear / Vercel / Raycast / Leica craft): deep obsidian canvas (`--bg: #090A0D`), stepped carbon surfaces (`--surface: #111318`, `--surface-2: #161920`), exposed `1px solid rgba(255,255,255,0.08)` architectural hairlines, crisp off-white primary ink (`--text: #F5F6F8`), calm slate secondary ink (`--muted: #8E95A5`), high-contrast off-white primary CTA button (`background: #F5F6F8; color: #090A0D; font-weight: 500`), and ONE restrained accent — never rainbow pills or purple-to-indigo SaaS gradients. When a light theme fits better, use warm gallery paper (`#F7F6F2`) with deep carbon ink (`#0B0C0E`) and subtle stone hairlines.
- **Arrange**: Use an explicit 8px spacing scale, deliberate asymmetric or editorial grids (exposed `1px` hairline bento / split-hero / gallery) instead of cookie-cutter 3-column icon cards, and maintain generous negative space between major sections (`64px–120px`).
- **Bolder / Quieter**: Spend visual drama in ONE focal moment (hero typography, rendered product, or signature interactive canvas); keep navigation, borders, and secondary controls calm and quiet.
- **Polish & Harden**: Refine hover/active/focus-visible states, custom range sliders & thin dark scrollbars, responsive stacking at `375px`/`768px`/`1440px`, empty/loading/error states, and real plausible domain data.

## Deterministic Anti-Slop Detector (Zero Tolerance)

Eliminate ALL of these AI-generated tells unless the user explicitly requests them:
1. **Color Slop**: Default violet/indigo/cyan mesh gradients, glowing blurred orbs behind heroes, gradient-clipped headlines (`background-clip: text`), neon `box-shadow` halos, pure `#000000` + pure `#FFFFFF` harsh contrast without tinted surface elevation.
2. **Typography Slop**: Unmodified Inter/Arial defaults, ALL-CAPS tracked eyebrows above every single heading, accenting one random word in italics/gradient inside every headline, `01 / 02 / 03` decorative numbers on non-sequential items.
3. **Component Slop**: Identical rounded-2xl cards with floating colored icon squares + bold title + 2-line blurb, `"New ✨"` / `"AI-Powered"` / `"Beta"` pills, emoji bullets (`🚀`, `✨`, `🔥`) in UI chrome, fake `99.9%` / `10k+` vanity metrics, stock placeholder portraits, pulsing green status dots.
4. **Layout & Motion Slop**: Horizontal scroll clipping (`overflow-x: auto` cutting off pills instead of `flex-wrap: wrap`), bounce/fade-up scroll reveals on every element, cramped padding (`<16px` inside cards), mismatched border radii across nested elements (`outerRadius = innerRadius + padding`).
5. **3D & Canvas Overlay Slop**: Unbacked dark text floating over a dark/split 3D `<canvas>` sky or dark 3D geometry (or light text over bright geometry), transparent sticky/fixed headers (`alpha < 0.78` without `backdrop-filter: blur(...)`) that let scrolled tables/text bleed behind navigation links, harsh two-tone horizon cuts behind typography, and generic "Saturn-ring + sphere" placeholder 3D toys. Every text block and sticky header over a canvas MUST sit on a calibrated translucent surface panel or guaranteed high-contrast safe zone (`>= 4.5:1` WCAG AA at every scroll position).

## Checklist

- [ ] Distinct product-native visual direction (palette tokens + intentional font pairing + clear focal hierarchy).
- [ ] Zero banned AI-slop tells (purple glows, emoji chrome, generic 3-col icon cards, gradient text, pulsing badges).
- [ ] Optical alignment, consistent 8px rhythm, nested radius math, and clean contrast verified on a live screenshot.
