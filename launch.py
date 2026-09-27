#!/usr/bin/env python3
"""launchkit - take a built product from "repo on GitHub" to "live, indexed, measured".

Commands
  check  <name> [name ...] [--tlds com,io,app]   availability + price (Cloudflare Registrar)
  launch <domain> --repo owner/repo [--buy]       buy -> DNS -> Vercel -> Search Console -> GA4 -> sitemap -> audit
  audit  <url>                                    stack-agnostic SEO / hygiene check of a live site
  report <domain> [--days 28]                     Search Console clicks, impressions, top queries

Every launch step is idempotent and recorded in state/<domain>.json, so a failed
run can simply be re-run. Standard library only (Python 3.9+).
"""
import argparse, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from html.parser import HTMLParser
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_DIR = HERE / "state"
UA = "Mozilla/5.0 (launchkit audit)"

# ---------------------------------------------------------------- utilities
def load_env():
    f = HERE / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

def env(name, required=True):
    v = os.environ.get(name, "")
    if required and not v:
        die(f"missing {name} in .env (see .env.example)")
    return v

def die(msg):
    print(f"\n  ERROR: {msg}\n"); sys.exit(1)

def log(step, msg):
    print(f"[{step:<9}] {msg}")

class HTTPError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:400]}")
        self.status, self.body = status, body

def http(method, url, token=None, body=None, headers=None, form=None, timeout=30):
    h = {"User-Agent": UA}
    if token: h["Authorization"] = f"Bearer {token}"
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode(); h["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode(); h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip().startswith(("{", "[")) else raw
    except urllib.error.HTTPError as e:
        raise HTTPError(e.code, e.read().decode("utf-8", "replace"))

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None

def fetch(url, follow=True, timeout=20):
    """Return (status, headers, text, final_url). Never raises on HTTP status."""
    opener = urllib.request.build_opener() if follow else urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read(3_000_000).decode("utf-8", "replace"), r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), "", url
    except Exception as e:  # DNS, TLS, timeout
        return 0, {}, str(e), url

def load_state(domain):
    STATE_DIR.mkdir(exist_ok=True)
    f = STATE_DIR / f"{domain}.json"
    return json.loads(f.read_text()) if f.exists() else {"domain": domain, "done": []}

def save_state(st):
    (STATE_DIR / f"{st['domain']}.json").write_text(json.dumps(st, indent=2))

def poll(fn, what, every=15, timeout=900):
    t0 = time.time()
    while True:
        r = fn()
        if r: return r
        if time.time() - t0 > timeout: die(f"timed out waiting for {what} - re-run later, it resumes")
        log("wait", f"{what} ... ({int(time.time()-t0)}s)"); time.sleep(every)

# ---------------------------------------------------------------- Cloudflare
CF = "https://api.cloudflare.com/client/v4"

def cf(method, path, body=None):
    r = http(method, CF + path, env("CF_API_TOKEN"), body)
    if isinstance(r, dict) and r.get("success") is False: die(f"Cloudflare {path}: {r.get('errors')}")
    return r.get("result") if isinstance(r, dict) and "result" in r else r

def cf_check(domains):
    acct = env("CF_ACCOUNT_ID"); out = []
    for i in range(0, len(domains), 20):
        r = cf("POST", f"/accounts/{acct}/registrar/domain-check", {"domains": domains[i:i+20]})
        out += (r.get("domains") if isinstance(r, dict) else r) or []
    return out

def cf_zone(domain):
    z = cf("GET", f"/zones?name={domain}&account.id={env('CF_ACCOUNT_ID')}")
    return z[0] if z else None

def cf_upsert(zone_id, rtype, name, content, match=None, proxied=False, priority=None):
    """Create or update one record. For TXT, `match` is a prefix so SPF and google-site-verification coexist."""
    recs = cf("GET", f"/zones/{zone_id}/dns_records?type={rtype}&name={name}") or []
    if match: recs = [r for r in recs if r["content"].strip('"').startswith(match)]
    body = {"type": rtype, "name": name, "content": content, "ttl": 1}
    if rtype in ("A", "AAAA", "CNAME"): body["proxied"] = proxied
    if priority is not None: body["priority"] = priority
    if recs:
        if recs[0]["content"].strip('"') == content and recs[0].get("proxied", proxied) == proxied: return "unchanged"
        cf("PUT", f"/zones/{zone_id}/dns_records/{recs[0]['id']}", body); return "updated"
    if rtype in ("A", "CNAME"):  # A and CNAME can't coexist on one name
        for other in cf("GET", f"/zones/{zone_id}/dns_records?name={name}") or []:
            if other["type"] in ("A", "AAAA", "CNAME"): cf("DELETE", f"/zones/{zone_id}/dns_records/{other['id']}")
    cf("POST", f"/zones/{zone_id}/dns_records", body); return "created"

# ---------------------------------------------------------------- Vercel
VC = "https://api.vercel.com"

def vc(method, path, body=None):
    team = os.environ.get("VERCEL_TEAM_ID")
    if team: path += ("&" if "?" in path else "?") + f"teamId={team}"
    return http(method, VC + path, env("VERCEL_TOKEN"), body)

# ---------------------------------------------------------------- Google
_gtok = {}
def gtoken():
    if _gtok.get("exp", 0) > time.time(): return _gtok["t"]
    r = http("POST", "https://oauth2.googleapis.com/token", form={
        "client_id": env("GOOGLE_CLIENT_ID"), "client_secret": env("GOOGLE_CLIENT_SECRET"),
        "refresh_token": env("GOOGLE_REFRESH_TOKEN"), "grant_type": "refresh_token"})
    _gtok.update(t=r["access_token"], exp=time.time() + r.get("expires_in", 3600) - 60)
    return _gtok["t"]

def g(method, url, body=None): return http(method, url, gtoken(), body)

def gsc_site(domain): return urllib.parse.quote(f"sc-domain:{domain}", safe="")

# ---------------------------------------------------------------- launch steps
def step(st, name):
    def deco(fn):
        def run(*a, **k):
            if name in st["done"]: log(name, "already done"); return
            if fn(*a, **k) is False: log(name, "not complete - will retry on next run"); return
            st["done"].append(name); save_state(st)
        return run
    return deco

def cmd_launch(a):
    d = a.domain.lower().strip(); st = load_state(d)
    canon = f"www.{d}" if a.canonical == "www" else d
    other = d if a.canonical == "www" else f"www.{d}"
    st.update(repo=a.repo, canonical=canon); save_state(st)
    print(f"\nLaunching {d}  (canonical https://{canon}, repo {a.repo})\n")

    @step(st, "domain")
    def s_domain():
        if cf_zone(d): log("domain", "zone already in this Cloudflare account - skipping purchase"); return
        info = cf_check([d]); info = info[0] if info else {}
        if not info.get("registrable"): die(f"{d} not registrable: {info.get('reason', info)}")
        p = info.get("pricing", {})
        log("domain", f"available, {p.get('registration_cost')} {p.get('currency')}/yr (renews {p.get('renewal_cost')})")
        if not a.buy: die("re-run with --buy to purchase (billed to your Cloudflare default payment method)")
        acct = env("CF_ACCOUNT_ID")
        r = cf("POST", f"/accounts/{acct}/registrar/registrations", {"domain_name": d, "auto_renew": True})
        log("domain", f"registration submitted: {r.get('state') if isinstance(r, dict) else r}")
        def done():
            s = cf("GET", f"/accounts/{acct}/registrar/registrations/{d}/registration-status")
            state = (s or {}).get("state", "") if isinstance(s, dict) else ""
            if state in ("failed", "rejected", "error"): die(f"registration failed: {s}")
            return (s or {}).get("completed") or state in ("succeeded", "active", "registered", "completed")
        poll(done, "registration", every=20, timeout=1800)

    @step(st, "zone")
    def s_zone():
        z = poll(lambda: cf_zone(d), "Cloudflare zone", every=20, timeout=900)
        st["zone_id"] = z["id"]; log("zone", f"zone {z['id']} status={z.get('status')}")

    @step(st, "vercel")
    def s_vercel():
        name = re.sub(r"[^a-z0-9-]", "-", d)[:100]
        try:
            p = vc("GET", f"/v9/projects/{name}")
        except HTTPError as e:
            if e.status != 404: raise
            body = {"name": name, "gitRepository": {"type": "github", "repo": a.repo}}
            if a.framework: body["framework"] = a.framework
            p = vc("POST", "/v11/projects", body); log("vercel", f"created project {name}")
        st["vercel_project"] = p["id"]; st["vercel_name"] = name
        for host, redirect in ((canon, None), (other, canon)):
            b = {"name": host}
            if redirect: b.update(redirect=redirect, redirectStatusCode=308)
            try:
                r = vc("POST", f"/v10/projects/{p['id']}/domains", b)
                st.setdefault("vercel_verification", []).extend(r.get("verification") or [])
                log("vercel", f"added {host}" + (f" -> {redirect}" if redirect else ""))
            except HTTPError as e:
                if e.status == 409: log("vercel", f"{host} already attached")
                else: raise

    @step(st, "dns")
    def s_dns():
        z = st["zone_id"]
        def rec(host):
            try: return vc("GET", f"/v6/domains/{host}/config?projectIdOrName={st['vercel_project']}")
            except HTTPError: return {}
        apex_cfg, www_cfg = rec(d), rec(f"www.{d}")
        ip = next((x["value"][0] for x in sorted(apex_cfg.get("recommendedIPv4", []), key=lambda x: x["rank"]) if x["value"]), "76.76.21.21")
        cname = next((x["value"] for x in sorted(www_cfg.get("recommendedCNAME", []), key=lambda x: x["rank"])), "cname.vercel-dns.com")
        log("dns", f"A {d} -> {ip}: {cf_upsert(z, 'A', d, ip)}")
        log("dns", f"CNAME www -> {cname.rstrip('.')}: {cf_upsert(z, 'CNAME', f'www.{d}', cname.rstrip('.'))}")
        for v in st.get("vercel_verification", []):
            if v.get("type") == "TXT":
                log("dns", f"TXT {v['domain']}: {cf_upsert(z, 'TXT', v['domain'], v['value'], match=v['value'][:12])}")
        if not a.email:  # domain sends no mail: stop anyone spoofing it
            log("dns", f"SPF -all: {cf_upsert(z, 'TXT', d, 'v=spf1 -all', match='v=spf1')}")
            log("dns", f"DMARC reject: {cf_upsert(z, 'TXT', f'_dmarc.{d}', 'v=DMARC1; p=reject;', match='v=DMARC1')}")

    @step(st, "gsc")
    def s_gsc():
        site = {"type": "INET_DOMAIN", "identifier": d}
        tok = g("POST", "https://www.googleapis.com/siteVerification/v1/token",
                {"site": site, "verificationMethod": "DNS_TXT"})["token"]
        log("gsc", f"TXT {d}: {cf_upsert(st['zone_id'], 'TXT', d, tok, match='google-site-verification')}")
        def verified():
            try:
                g("POST", "https://www.googleapis.com/siteVerification/v1/webResource?verificationMethod=DNS_TXT", {"site": site})
                return True
            except HTTPError as e:
                if e.status in (400, 403): return False
                raise
        poll(verified, "Google DNS verification", every=30, timeout=1800)
        g("PUT", f"https://www.googleapis.com/webmasters/v3/sites/{gsc_site(d)}")
        log("gsc", f"Search Console domain property sc-domain:{d} added")

    @step(st, "ga4")
    def s_ga4():
        if a.no_ga: log("ga4", "skipped (--no-ga)"); return
        base = "https://analyticsadmin.googleapis.com/v1beta"
        if "ga4_property" not in st:  # saved immediately so a re-run never creates a duplicate property
            prop = g("POST", f"{base}/properties", {"parent": f"accounts/{env('GA4_ACCOUNT_ID')}", "displayName": d,
                                                       "timeZone": a.tz, "currencyCode": "USD"})
            st["ga4_property"] = prop["name"]; save_state(st)
        prop = {"name": st["ga4_property"]}
        stream = g("POST", f"{base}/{prop['name']}/dataStreams", {"type": "WEB_DATA_STREAM", "displayName": d,
                                                                  "webStreamData": {"defaultUri": f"https://{canon}"}})
        mid = stream["webStreamData"]["measurementId"]; st["ga4"] = {"property": prop["name"], "measurement_id": mid}
        vc("POST", f"/v10/projects/{st['vercel_project']}/env?upsert=true",
           {"key": a.ga_env, "value": mid, "type": "plain", "target": ["production", "preview"]})
        log("ga4", f"{prop['name']} stream {mid} -> Vercel env {a.ga_env}")

    @step(st, "deploy")
    def s_deploy():
        p = vc("GET", f"/v9/projects/{st['vercel_project']}"); link = p.get("link") or {}
        if not link.get("repoId"): log("deploy", "project has no git link yet - push a commit to deploy"); return False
        r = vc("POST", "/v13/deployments", {"name": st["vercel_name"], "project": st["vercel_project"], "target": "production",
               "gitSource": {"type": "github", "repoId": link["repoId"], "ref": link.get("productionBranch") or "main"}})
        log("deploy", f"production deployment {r.get('url')} queued")

    @step(st, "live")
    def s_live():
        poll(lambda: fetch(f"https://{canon}/")[0] == 200, f"https://{canon} to return 200", every=30, timeout=1800)
        log("live", f"https://{canon} is up")

    @step(st, "sitemap")
    def s_sitemap():
        sm = f"https://{canon}{a.sitemap}"
        if fetch(sm)[0] != 200: log("sitemap", f"{sm} not found - add one to the repo, then re-run"); return False
        g("PUT", f"https://www.googleapis.com/webmasters/v3/sites/{gsc_site(d)}/sitemaps/{urllib.parse.quote(sm, safe='')}")
        log("sitemap", f"submitted {sm} to Search Console")
        key = os.environ.get("INDEXNOW_KEY")
        if key and fetch(f"https://{canon}/{key}.txt")[0] == 200:
            urls = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", fetch(sm)[2])[:10000]
            try:
                http("POST", "https://api.indexnow.org/indexnow", body={"host": canon, "key": key, "urlList": urls})
                log("sitemap", f"IndexNow (Bing, Yandex, ...) pinged with {len(urls)} URLs")
            except HTTPError as e: log("sitemap", f"IndexNow failed: {e}")
        else: log("sitemap", "IndexNow skipped (INDEXNOW_KEY unset or /<key>.txt not served)")

    for s in (s_domain, s_zone, s_vercel, s_dns, s_gsc, s_ga4, s_deploy, s_live, s_sitemap): s()
    print(); fails = run_audit(f"https://{canon}", quiet=False)
    print(f"\nDone. State: state/{d}.json. {'Fix the FAIL lines above.' if fails else 'All critical checks passed.'}")
    print("One-time manual: in Bing Webmaster Tools, 'Import from Google Search Console' (covers all future sites).")

# ---------------------------------------------------------------- audit
class Head(HTMLParser):
    def __init__(self):
        super().__init__(); self.meta, self.links, self.title, self._t, self.lang, self.jsonld, self.scripts, self.a = {}, {}, "", False, None, 0, [], []
    def handle_starttag(self, tag, attrs):
        at = dict(attrs)
        if tag == "html": self.lang = at.get("lang")
        elif tag == "title": self._t = True
        elif tag == "meta":
            k = (at.get("name") or at.get("property") or "").lower()
            if k: self.meta[k] = at.get("content", "")
        elif tag == "link":
            for rel in (at.get("rel") or "").lower().split(): self.links.setdefault(rel, at.get("href", ""))
        elif tag == "script":
            if at.get("type") == "application/ld+json": self.jsonld += 1
            if at.get("src"): self.scripts.append(at["src"])
        elif tag == "a" and at.get("href"): self.a.append(at["href"].lower())
    def handle_endtag(self, tag):
        if tag == "title": self._t = False
    def handle_data(self, data):
        if self._t: self.title += data

def run_audit(url, quiet=False):
    url = url.rstrip("/"); pu = urllib.parse.urlparse(url); host = pu.netloc
    res = []
    def r(level, check, detail=""): res.append((level, check, detail))
    status, hdr, html, final = fetch(url + "/")
    if status != 200: r("FAIL", "homepage", f"status {status} {html[:120] if not status else ''}"); return report_audit(res, quiet)
    r("PASS", "homepage 200")
    hl = {k.lower(): v for k, v in hdr.items()}
    # redirects
    alt = host[4:] if host.startswith("www.") else f"www.{host}"
    s, h, _, _ = fetch(f"https://{alt}/", follow=False)
    loc = {k.lower(): v for k, v in h.items()}.get("location", "")
    r("PASS" if s in (301, 308) and host in loc else "FAIL", f"{alt} redirects to canonical", f"{s} -> {loc or 'none'}")
    s, h, _, _ = fetch(f"http://{host}/", follow=False)
    r("PASS" if s in (301, 302, 307, 308) else "WARN", "http -> https", str(s))
    r("PASS" if "strict-transport-security" in hl else "WARN", "HSTS header")
    p = Head(); p.feed(html); m = p.meta; title = p.title.strip()
    r("PASS" if 10 <= len(title) <= 65 else "WARN", "title length", f"{len(title)} chars: {title[:70]}")
    desc = m.get("description", "")
    r("PASS" if 50 <= len(desc) <= 165 else ("FAIL" if not desc else "WARN"), "meta description", f"{len(desc)} chars")
    can = p.links.get("canonical", "")
    r("PASS" if can.rstrip("/") == url else ("FAIL" if not can else "WARN"), "canonical", can or "missing")
    r("FAIL" if "noindex" in m.get("robots", "").lower() else "PASS", "not noindex")
    r("PASS" if p.lang else "WARN", "html lang", p.lang or "missing")
    r("PASS" if "viewport" in m else "FAIL", "viewport meta")
    for k in ("og:title", "og:description", "twitter:card"): r("PASS" if m.get(k) else "WARN", k)
    ogi = m.get("og:image", "")
    r("PASS" if ogi and fetch(urllib.parse.urljoin(url + "/", ogi))[0] == 200 else "FAIL", "og:image loads", ogi or "missing")
    fav = p.links.get("icon") or p.links.get("shortcut") or "/favicon.ico"
    r("PASS" if fetch(urllib.parse.urljoin(url + "/", fav))[0] == 200 else "WARN", "favicon")
    r("PASS" if p.jsonld else "WARN", "JSON-LD structured data", f"{p.jsonld} block(s)")
    blob = html.lower() + " ".join(p.scripts).lower()
    found = [n for n, sig in (("GA4", "googletagmanager.com/gtag"), ("GTM", "googletagmanager.com/gtm"), ("Vercel", "/_vercel/insights"),
             ("Plausible", "plausible"), ("PostHog", "posthog"), ("Cloudflare", "cloudflareinsights")) if sig in blob]
    r("PASS" if found else "WARN", "analytics tag in HTML", ", ".join(found) or "none seen (client-injected tags may not show)")
    for page in ("privacy", "terms"):
        r("PASS" if any(page in a for a in p.a) else "WARN", f"{page} page linked")
    # robots + sitemap
    s, _, robots, _ = fetch(url + "/robots.txt")
    if s != 200: r("FAIL", "robots.txt", f"status {s}")
    else:
        r("FAIL" if re.search(r"(?im)^disallow:\s*/\s*$", robots) else "PASS", "robots.txt allows crawling")
        r("PASS" if "sitemap:" in robots.lower() else "WARN", "robots.txt lists sitemap")
    sm = (re.findall(r"(?im)^sitemap:\s*(\S+)", robots or "") or [url + "/sitemap.xml"])[0]
    s, _, smx, _ = fetch(sm)
    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", smx)
    r("PASS" if s == 200 and locs else "FAIL", "sitemap", f"{len(locs)} URLs" if locs else f"status {s}")
    bad = [u for u in locs[:8] if fetch(u)[0] != 200]
    if locs: r("PASS" if not bad else "FAIL", "sample sitemap URLs return 200", ", ".join(bad))
    offhost = [u for u in locs if urllib.parse.urlparse(u).netloc != host]
    if locs: r("PASS" if not offhost else "FAIL", "sitemap URLs use canonical host", f"{len(offhost)} off-host")
    s = fetch(url + "/this-page-should-not-exist-lk")[0]
    r("PASS" if s == 404 else "FAIL", "unknown URL returns 404", str(s))
    r("INFO", "llms.txt", "present" if fetch(url + "/llms.txt")[0] == 200 else "absent (optional)")
    return report_audit(res, quiet)

def report_audit(res, quiet):
    order = {"FAIL": 0, "WARN": 1, "PASS": 2, "INFO": 3}
    for lvl, c, d in sorted(res, key=lambda x: order[x[0]]):
        if not quiet or lvl != "PASS": print(f"  {lvl:<4}  {c}" + (f"  - {d}" if d else ""))
    return sum(1 for x in res if x[0] == "FAIL")

# ---------------------------------------------------------------- other commands
def cmd_check(a):
    tlds = [t.strip(".") for t in a.tlds.split(",")]
    names = [n.lower() if "." in n else f"{n.lower()}.{t}" for n in a.names for t in ([None] if "." in n else tlds)]
    rows = cf_check(names)
    print(f"\n  {'domain':<32} {'available':<10} {'1st yr':>8} {'renew':>8}")
    for r in sorted(rows, key=lambda x: (not x.get("registrable"), x.get("name", ""))):
        p = r.get("pricing") or {}
        print(f"  {r.get('name',''):<32} {'yes' if r.get('registrable') else 'no':<10} {str(p.get('registration_cost','-')):>8} {str(p.get('renewal_cost','-')):>8}"
              + ("" if r.get("registrable") else f"  {r.get('reason','')}"))
    print("\n  (Only TLDs in the Cloudflare Registrar API beta can be checked/bought this way.)\n")

def cmd_report(a):
    from datetime import date, timedelta
    end = date.today() - timedelta(days=2); start = end - timedelta(days=a.days)
    base = f"https://www.googleapis.com/webmasters/v3/sites/{gsc_site(a.domain)}"
    tot = g("POST", f"{base}/searchAnalytics/query", {"startDate": str(start), "endDate": str(end)}).get("rows", [{}])[0]
    print(f"\n{a.domain}  {start} .. {end}")
    print(f"  clicks {tot.get('clicks',0):.0f}  impressions {tot.get('impressions',0):.0f}  CTR {tot.get('ctr',0)*100:.1f}%  avg pos {tot.get('position',0):.1f}")
    for dim in ("query", "page"):
        rows = g("POST", f"{base}/searchAnalytics/query", {"startDate": str(start), "endDate": str(end),
                 "dimensions": [dim], "rowLimit": 10}).get("rows", [])
        print(f"\n  top {dim}s"); [print(f"    {x['clicks']:>5.0f} clk {x['impressions']:>7.0f} imp  {x['keys'][0]}") for x in rows]
    for s in g("GET", f"{base}/sitemaps").get("sitemap", []):
        print(f"\n  sitemap {s['path']}  errors={s.get('errors',0)} warnings={s.get('warnings',0)} last read {s.get('lastDownloaded','never')}")

def main():
    load_env()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check"); c.add_argument("names", nargs="+"); c.add_argument("--tlds", default="com,io,app,co")
    l = sub.add_parser("launch"); l.add_argument("domain"); l.add_argument("--repo", required=True, help="GitHub owner/repo")
    l.add_argument("--buy", action="store_true", help="allow purchasing the domain")
    l.add_argument("--canonical", choices=["www", "apex"], default="www")
    l.add_argument("--framework", default="nextjs", help="Vercel framework preset; '' for auto/other")
    l.add_argument("--sitemap", default="/sitemap.xml"); l.add_argument("--no-ga", action="store_true")
    l.add_argument("--ga-env", default="NEXT_PUBLIC_GA_ID", help="env var name your code reads the GA4 ID from")
    l.add_argument("--tz", default="Asia/Kolkata", help="GA4 reporting time zone")
    l.add_argument("--email", action="store_true", help="domain will send mail: skip SPF -all / DMARC reject")
    au = sub.add_parser("audit"); au.add_argument("url")
    rp = sub.add_parser("report"); rp.add_argument("domain"); rp.add_argument("--days", type=int, default=28)
    a = ap.parse_args()
    if a.cmd == "audit":
        u = a.url if a.url.startswith("http") else f"https://{a.url}"
        print(f"\nAudit {u}\n"); sys.exit(1 if run_audit(u) else 0)
    {"check": cmd_check, "launch": cmd_launch, "report": cmd_report}[a.cmd](a)

if __name__ == "__main__":
    main()
