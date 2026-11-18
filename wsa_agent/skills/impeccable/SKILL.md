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
- **Colorize**: Establish a disciplined token palette (`--bg`, `--surface`, `--surface-2`, `--border`, `--text`, `--muted`, `--accent`) using tinted neutrals (warm stone, cool slate, deep obsidian) with ONE purposeful accent — never rainbow pills or purple-to-indigo SaaS gradients.
- **Arrange**: Use an explicit 8px spacing scale, deliberate asymmetric or editorial grids (bento / split-hero / hairline gallery) instead of cookie-cutter 3-column icon cards, and maintain generous negative space between major sections (`64px–120px`).
- **Bolder / Quieter**: Spend visual drama in ONE focal moment (hero typography, rendered product, or signature interactive canvas); keep navigation, borders, and secondary controls calm and quiet.
- **Polish & Harden**: Refine hover/active/focus-visible states, custom range sliders & thin dark scrollbars, responsive stacking at `375px`/`768px`/`1440px`, empty/loading/error states, and real plausible domain data.

## Deterministic Anti-Slop Detector (Zero Tolerance)

Eliminate ALL of these AI-generated tells unless the user explicitly requests them:
1. **Color Slop**: Default violet/indigo/cyan mesh gradients, glowing blurred orbs behind heroes, gradient-clipped headlines (`background-clip: text`), neon `box-shadow` halos, pure `#000000` + pure `#FFFFFF` harsh contrast without tinted surface elevation.
2. **Typography Slop**: Unmodified Inter/Arial defaults, ALL-CAPS tracked eyebrows above every single heading, accenting one random word in italics/gradient inside every headline, `01 / 02 / 03` decorative numbers on non-sequential items.
3. **Component Slop**: Identical rounded-2xl cards with floating colored icon squares + bold title + 2-line blurb, `"New ✨"` / `"AI-Powered"` / `"Beta"` pills, emoji bullets (`🚀`, `✨`, `🔥`) in UI chrome, fake `99.9%` / `10k+` vanity metrics, stock placeholder portraits, pulsing green status dots.
4. **Layout & Motion Slop**: Horizontal scroll clipping (`overflow-x: auto` cutting off pills instead of `flex-wrap: wrap`), bounce/fade-up scroll reveals on every element, cramped padding (`<16px` inside cards), mismatched border radii across nested elements (`outerRadius = innerRadius + padding`).

## Checklist

- [ ] Distinct product-native visual direction (palette tokens + intentional font pairing + clear focal hierarchy).
- [ ] Zero banned AI-slop tells (purple glows, emoji chrome, generic 3-col icon cards, gradient text, pulsing badges).
- [ ] Optical alignment, consistent 8px rhythm, nested radius math, and clean contrast verified on a live screenshot.
