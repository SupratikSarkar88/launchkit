# launchkit

**You built it with AI. Now ship it.** One command takes a finished web app from a GitHub repo to a live site that Google can find: domain, DNS, hosting, Search Console, analytics, sitemap, and a launch audit.

```bash
python3 launch.py suggest invoice tool                  # available name ideas + price
python3 launch.py check myapp coolname                  # check names you already have
python3 launch.py launch myapp.com --repo you/myapp --buy
python3 launch.py audit myapp.com                       # any site, any stack
python3 launch.py report myapp.com                      # Search Console clicks + top queries
```

**The easy way:**

```bash
python3 launch.py yourdomain.com
```

It asks which GitHub repo and four yes/no questions. The first time only, it opens Cloudflare, Vercel and Google in your browser, tells you what to click and saves the access. It shows the price, and after you type `BUY` it does everything else. `python3 launch.py` alone opens a menu (suggest names, check names, launch, audit, report).

Not a coder? Install it as a Claude plugin and say *"launch my app"*. Claude walks you through the rest (see [Use with Claude](#use-with-claude)).

## What `launch` does

| # | Step | How |
|---|------|-----|
| 1 | Check price, buy the domain (auto-renew on, WHOIS redacted) | Cloudflare Registrar API |
| 2 | Wait for the DNS zone | Cloudflare API |
| 3 | Create a Vercel project from your GitHub repo; attach `www` + apex, apex redirects to `www` | Vercel API |
| 4 | DNS records to Vercel; SPF/DMARC so nobody can spoof email from your domain | Cloudflare API |
| 5 | Verify the domain with Google; add it to Search Console | Site Verification + Search Console APIs |
| 6 | Create Google Analytics 4 and pass its ID to your app (`NEXT_PUBLIC_GA_ID`) | GA Admin API + Vercel API |
| 6b | *Optional* `--posthog`: pass a PostHog project key to your app (`NEXT_PUBLIC_POSTHOG_KEY` / `_HOST`) for product analytics and session replay | Vercel API (+ PostHog API with `--posthog-new`) |
| 7 | Deploy to production | Vercel API |
| 8 | Wait until live; submit the sitemap; ping Bing via IndexNow | Search Console API, IndexNow |
| 9 | Audit: titles, descriptions, canonical, social cards, robots, sitemap, redirects, 404s | plain HTTP |

Every step is saved to `state/<domain>.json`. If anything fails, run the same command again and it continues where it stopped. Python 3.9+, no dependencies.

## One-time setup (~30 minutes)

Run `python3 launch.py setup` (or just launch a domain). It walks you through each item below in the browser and saves the keys to `.env`. You can still fill `.env` by hand from `.env.example`.

1. **Cloudflare** API token (the script says which permissions).
2. **Cloudflare** → Registrar: add a payment method and default contact, accept the agreement.
3. **Vercel** → install the GitHub app with access to your repos.
4. **Google Cloud** → enable Site Verification, Search Console and Analytics Admin APIs. OAuth consent screen: *External*, then **Publish** (in Testing mode the login expires after 7 days). Create a *Desktop* OAuth client, then:
   The script opens each page and then signs you in to Google. Nothing to install.
5. **Bing Webmaster Tools** → "Import from Google Search Console". Once, covers every future site.

## What your app must include

launchkit checks these but can't write your code. Ask your AI builder for them before launching:

- `sitemap.xml` and `robots.txt` (with a `Sitemap:` line)
- a unique `<title>`, meta description and canonical URL per page
- Open Graph tags + a 1200×630 share image, `twitter:card`, `<html lang>`
- analytics snippet reading `NEXT_PUBLIC_GA_ID` (and, if you use `--posthog`, the PostHog snippet reading `NEXT_PUBLIC_POSTHOG_KEY` / `NEXT_PUBLIC_POSTHOG_HOST`)
- `/privacy` and `/terms` pages linked in the footer; unknown URLs return a real 404

## Use with Claude

This repo is also a Claude plugin (`.claude-plugin/` + `skills/launch-product`). Add it, point Claude at the folder, and ask it to launch your app. It checks your repo first, shows you domain prices, and **never buys anything without your explicit yes**.

## Good to know

- **Vercel's free Hobby plan is non-commercial only.** Once your app takes payments, move to Pro.
- **GA4 uses cookies.** EU/UK visitors need a consent banner. Skip it with `--no-ga` if you use cookieless analytics.
- **Cloudflare's registration API is in beta**: not every TLD is available, and renewals/transfers are dashboard-only.
- **PostHog's free plan allows one project.** `--posthog` reuses it for every app (split them by the `$host` property); `--posthog-new` creates a project per app and needs a paid PostHog org.
- Flags: `--posthog`, `--posthog-new`, `--canonical apex`, `--email` (domain will send mail), `--framework ''` (non-Next.js), `--no-ga`, `--tz`.

## License

MIT. Built by [@ThinkSupratik](https://thinksupratik.com).
