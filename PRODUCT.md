# Shopping app
<!-- uizze:product-schema 1 -->

## Platform
Telegram primary; optional local web

## Stack
Python Telegram voice/text handlers use the existing validated planner, SQLite and Browser Use shopping workflow. ElevenLabs supplies Arabic/English transcription and optional spoken audio. The optional local Cloudflare Think voice page remains stopped while Telegram owns the profile.

## Users
Private Telegram use by the authorized shopping-app owner, with Arabic, English and mixed voice input, inline approval and spoken replies.

## Product Purpose
Speak or type groceries, review the full merged shopping list and exact changes, press Run shop, then review cart results before manual checkout.

## Capabilities and Constraints
Existing shopping.py workflow and SQLite remain authoritative. Quantities mean purchasable units. Preserve live cart items. Block list changes during shopping. Never retry shopping automatically or place orders. Keep credentials and diagnostics private. Stop Telegram before using the shared browser profile from the web app.

## Product Principles
- List-first, quick quantity editing.
- Clear incomplete versus verified outcomes.
- Explicit human purchase confirmation.
