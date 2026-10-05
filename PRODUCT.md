# Shopping app
<!-- uizze:product-schema 1 -->

## Platform
web

## Stack
Confirmed: lightweight Python web server and plain HTML/CSS/JavaScript; replace Telegram as the active shopping controller.

## Users
Private, English-language phone use by the existing shopping-app owner.

## Product Purpose
Edit a grocery list without commands, prepare a Tamimi cart, and review verified results before manual checkout.

## Capabilities and Constraints
Existing shopping.py workflow and SQLite remain authoritative. Quantities mean purchasable units. Preserve live cart items. Block list changes during shopping. Never retry shopping automatically or place orders. Keep credentials and diagnostics private. Stop Telegram before using the shared browser profile from the web app.

## Product Principles
- List-first, quick quantity editing.
- Clear incomplete versus verified outcomes.
- Explicit human purchase confirmation.
