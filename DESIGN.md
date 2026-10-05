---
name: Shopping
description: Current visual system for the private English grocery-list app.
colors:
  green: "#164d3b"
  green-hover: "#0c392a"
  canvas: "#f3f6f5"
  surface: "#ffffff"
  text: "#202e29"
  muted: "#586860"
  line: "#d5dfda"
  input-border: "#91a59a"
  placeholder: "#62746a"
  focus: "#28795c"
  hover-surface: "#e3ede7"
  danger: "#8c342c"
  warning: "#81551a"
  stale-surface: "#fff3dc"
  stale-text: "#64440f"
typography:
  body:
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif'
    fontSize: "16px"
    lineHeight: 1.5
  headline:
    fontSize: "clamp(1.65rem, 5vw, 2rem)"
    fontWeight: 650
    lineHeight: 1.2
    letterSpacing: "-.025em"
  title:
    fontSize: "1.2rem"
    fontWeight: 650
    lineHeight: 1.35
  label:
    fontSize: ".875rem"
    fontWeight: 600
rounded:
  input: "6px"
  button: "7px"
---

# Design System: Shopping

## Overview

Extracted from `web/index.html`, `web/style.css`, and `web/app.js`. Cool light surfaces, forest-green actions, system typography, and ruled grocery rows support private phone use. This records the implementation, not a proposed redesign.

## Colors

Green identifies actions and ordinary run status; muted text carries supporting instructions. White fields sit on the light canvas. Red marks destructive actions and errors; amber marks incomplete or stale states. Status text explains meaning independently of color.

## Typography

One system sans-serif stack throughout. Section titles use the title role; row names use 650 weight. Supporting labels are .875rem, hints .8125rem, and run references .75rem. Quantity numerals are tabular. Login heading is 2rem.

## Layout

Centered container: `min(100% - 32px, 1000px)`. Header is at least 80px tall. Mobile stacks the list before cart review. At 760px, columns use a 1.7:1 ratio with a 40px gap and a divided review column.

Add form uses a flexible item field, 62px quantity field, and action button with 8px gaps. Rows use 18px top and 10px bottom padding; names can wrap while quantity controls retain their width. Preferences expand inline.

Preparation form is sticky at the viewport bottom, with an opaque canvas background and safe-area bottom padding on mobile. Desktop places location and the 320px action area side by side. Document scroll padding is 220px. Full-page screenshots can capture the sticky bar across otherwise scrollable content.

## Elevation & Depth

Flat surfaces, thin separators, and tonal state backgrounds; no shadows or decorative blur.

## Shapes

Slightly rounded rectangular buttons and fields. Grocery rows remain unboxed. The wordmark has a small circular green dot.

## Components

- **Controls:** Minimum 44px height; quantity buttons are 44px wide, preparation button at least 48px tall. Primary buttons use white on green; secondary buttons use green on white; quiet buttons are transparent. Hover changes background. Disabled buttons use .48 opacity.
- **Focus and browser surfaces:** Visible 3px focus outline with 3px offset; themed selection, caret, and scrollbar. Native buttons, labeled inputs, and disclosure controls retain keyboard semantics. No authored animation; reduced-motion rules suppress motion.
- **List:** Empty guidance, editable quantities from 1–999, inline product preferences and approved alternatives. Removal and clearing use native confirmation dialogs.
- **Connection and authentication:** Initial loading, private sign-in, sign-in error, session expiry, and stale-connection retry messages. Stale, busy, and shopping states disable mutations. Feedback uses polite live regions.
- **Cart review:** No-run guidance, preparing, complete, incomplete, and failed states. Results show delivery slot, recorded products, unit prices, selections, unresolved items, and run reference. Missing data is stated explicitly. Purchase confirmation is separate from preparation and uses a native confirmation dialog.

## Do's and Don'ts

- Do preserve visible labels, readable contrast, touch-sized controls, and explicit state/recovery copy.
- Do keep incomplete results distinct from verified completion.
- Don't imply that preparing a cart places an order.
- Don't replace the functional list with decorative cards or dashboard metrics.
