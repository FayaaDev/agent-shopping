---
name: Shopping
description: Current visual system for the private bilingual voice grocery-list app.
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

Implemented in `voice-agent/index.html`, `voice-agent/src/style.css`, and `voice-agent/src/client.ts`. The voice surface preserves the original cool light surfaces, forest-green actions, system typography, and ruled grocery rows. Arabic/mixed content uses automatic text direction.

## Colors

Green identifies actions and ordinary run status; muted text carries supporting instructions. White fields sit on the light canvas. Red marks destructive actions and errors; amber marks incomplete or stale states. Status text explains meaning independently of color.

## Typography

One system sans-serif stack throughout. Section titles use the title role; row names use 650 weight. Supporting labels are .875rem, hints .8125rem, and run references .75rem. Quantity numerals are tabular. Login heading is 2rem.

## Layout

Centered container: `min(100% - 32px, 1000px)`. Header is at least 80px tall. Mobile stacks the list before cart review. At 760px, columns use a 1.7:1 ratio with a 40px gap and a divided review column.

Voice controls precede a labeled, editable request field and conversation transcript. Merged grocery rows wrap names while retaining visible target quantities. Saved preferences, alternatives and explicit caps appear inline beneath each row; proposed changes follow the list.

Approval is sticky at the viewport bottom, with an opaque canvas background and safe-area bottom padding. It pairs Run shop with the preview's approval conditions and the stop-before-order notice. Desktop splits transcript/conversation from merged list/results; mobile stacks them.

## Elevation & Depth

Flat surfaces, thin separators, and tonal state backgrounds; no shadows or decorative blur.

## Shapes

Slightly rounded rectangular buttons and fields. Grocery rows remain unboxed. The wordmark has a small circular green dot.

## Components

- **Controls:** Minimum 44px height; quantity buttons are 44px wide, preparation button at least 48px tall. Primary buttons use white on green; secondary buttons use green on white; quiet buttons are transparent. Hover changes background. Disabled buttons use .48 opacity.
- **Focus and browser surfaces:** Visible 3px focus outline with 3px offset; themed selection, caret, and scrollbar. Native buttons, labeled inputs, and disclosure controls retain keyboard semantics. No authored animation; reduced-motion rules suppress motion.
- **List:** Empty guidance, exact preview changes, target quantities, inline product preferences and approved alternatives. Edits happen through the voice/text request and require a regenerated preview.
- **Connection and authentication:** Automatic same-origin localhost session, loading, missing-provider guidance, microphone permission denial, clarification, session expiry and explicit connection recovery. Busy, stale and active-microphone states disable approval. Questions are displayed separately from technical error mapping.
- **Cart review:** No-run, preparing, completed, incomplete, failed and interrupted states; sanitized textual results and run reference. Read result aloud has a pending state; muting cancels pending playback. Purchase confirmation remains a separate CLI action.

## Do's and Don'ts

- Do preserve visible labels, readable contrast, touch-sized controls, and explicit state/recovery copy.
- Do keep incomplete results distinct from verified completion.
- Don't imply that preparing a cart places an order.
- Don't replace the functional list with decorative cards or dashboard metrics.
