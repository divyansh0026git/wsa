---
name: web-design-guidelines
description: Pre-ship UI/UX and accessibility audit covering 100+ rules across WCAG contrast, keyboard focus, semantic HTML/ARIA, forms, touch targets, motion, layout stability (CLS), dark/light coherence, and resilient states.
source: https://github.com/vercel-labs/agent-skills
category: ui-design
license: MIT (adapted for WSA)
---

# Web Design Guidelines — Pre-Ship UI/UX & Accessibility Audit

Inspired by `vercel-labs/agent-skills` (`web-design-guidelines`). Audit every website and web app against production UX, accessibility, and browser ergonomics before shipping.

## Core Audit Categories

1. **Accessibility & Keyboard Navigation**:
   - Every interactive control uses native semantics (`<button>` for actions, `<a href>` for navigation — never clickable `<div>`/`<span>` without role/tabindex/keyboard handlers).
   - Visible `:focus-visible` outlines (`outline: 2px solid var(--accent); outline-offset: 2px`) on every focusable element; never strip `outline: none` without a clear replacement.
   - Include a skip-to-content link on multi-section pages, proper heading hierarchy (`h1` -> `h2` -> `h3` without skipped levels), meaningful `alt` text on images (`alt=""` for decorative SVGs with `aria-hidden="true"`), and `aria-label` on icon-only buttons.
   - Modals, drawers, and popovers trap/restore focus cleanly, close on `Escape` and backdrop click, and set `aria-modal="true"` / `aria-expanded`.
2. **Color Contrast, Dark Mode & Visual Clarity**:
   - Body text meets WCAG AA (`>= 4.5:1`), large text/UI controls meet `>= 3:1` in BOTH light and dark themes.
   - Set `color-scheme: dark` (or `light dark`) so native scrollbars, form controls, and select dropdowns match the theme cleanly.
   - Never rely on color alone to convey status/errors — pair color with explicit icons or labels.
3. **Forms, Controls & Feedback**:
   - Every `<input>`, `<select>`, and `<textarea>` has an explicit `<label for>` (placeholder text is never a substitute for a label).
   - Provide inline, accessible validation (`aria-invalid`, `aria-describedby`) explaining how to fix the error; disable submit with clear pending state during async requests to prevent double-submits.
   - Touch targets are at least `40–44px` on mobile/pointer-coarse devices; buttons use descriptive verbs ("Save changes", "Add to bag"), never vague "Submit"/"OK".
4. **Layout Stability, Typography & Responsive Ergonomics**:
   - Zero horizontal page overflow (`document.documentElement.scrollWidth <= window.innerWidth`) at `375px`, `768px`, and `1440px`.
   - Reserve explicit `width`/`height` or `aspect-ratio` on images, videos, and 3D canvases to guarantee zero Cumulative Layout Shift (CLS).
   - Use `font-variant-numeric: tabular-nums` on dynamic counters, timers, tables, and prices so numbers never jitter.
   - Honor `@media (prefers-reduced-motion: reduce)` by disabling non-essential motion and smooth-scroll jumps.
5. **Resilient States & URL Hygiene**:
   - Every view handles loading, empty, error, and long-text truncation (`text-overflow: ellipsis` / `overflow-wrap: anywhere`) cleanly without breaking grids.
   - Report findings in terse `file:line` format when auditing and fix every high-impact failure before concluding.
