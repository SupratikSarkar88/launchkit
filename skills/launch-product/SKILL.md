---
name: launch-product
description: Take a finished web app live with launchkit - pick and buy a domain, wire DNS and Vercel hosting, Search Console, GA4, submit the sitemap, and audit. Use when the user says launch, ship, go live, or publish their app.
---

# Launch an app with launchkit

launchkit is the `launch.py` script in this plugin's folder. Run it from that folder on the user's own computer, where their filled `.env` lives. Many users are not programmers: explain each step in plain words, one step at a time.

## 0. Setup check
If `.env` is missing or has empty keys, walk the user through README "One-time setup" one service at a time. Never ask them to paste a secret into the chat; they type it into `.env` themselves. Never print `.env` contents.

## 1. Pre-flight the app (before spending money)
Open their repo and check it ships: `sitemap.xml`, `robots.txt` with a `Sitemap:` line, per-page title / meta description / canonical, Open Graph tags with a 1200x630 image, `twitter:card`, `<html lang>`, an analytics snippet reading `NEXT_PUBLIC_GA_ID`, `/privacy` and `/terms` linked, and a real 404 for unknown URLs. List what's missing, offer to add it, and get it committed and pushed first.

## 2. Pick the name
Ask for 3-10 name ideas (or suggest some from what the app does). Run `python3 launch.py check <names...>` and show the table. The user chooses.

## 3. Launch
State the exact domain and first-year price and get an explicit "yes, buy it". Only then run:
`python3 launch.py launch <domain> --repo <owner/repo> --buy`
Add `--email` if the domain will send mail, `--no-ga` if they don't want Google Analytics, `--framework ''` if the app is not Next.js.
Waiting steps (registration, Google verification, going live) can take many minutes. If the command stops or times out, run the same command again; it resumes.

## 4. Finish
Read the audit at the end. Fix any FAIL items in the repo, redeploy, and re-run `python3 launch.py audit <domain>` until there are none. Then tell the user in a few lines: live URL, that Google Search Console and Analytics are set up, and anything still marked WARN.

## Rules
- Never run with `--buy` without explicit confirmation of the exact domain and price. Purchases can't be undone.
- Don't read, print or send API tokens.
- If the app will take payments, mention once that Vercel's free plan is non-commercial only.
- If GA4 is on and they expect EU/UK visitors, mention once that a cookie consent banner is needed.
