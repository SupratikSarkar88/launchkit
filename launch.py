#!/usr/bin/env python3
"""launchkit - take a built product from "repo on GitHub" to "live, indexed, measured".

Easiest:  python3 launch.py FireEnough.com     (asks for anything it needs, then does everything)
          python3 launch.py                     (menu)

Commands
  suggest <keywords ...> [--limit 20]            available domain ideas + price (Cloudflare Registrar)
  check  <name> [name ...] [--tlds com,io,app]   availability + price for names you already have
  launch <domain> --repo owner/repo [--buy]       buy -> DNS -> Vercel -> Search Console -> GA4 [-> PostHog] -> sitemap -> audit
  connect <domain> [--canonical apex|www] [--no-ga] [--tz Asia/Kolkata]
                                                  site already live on any host: Search Console -> GA4 -> sitemap -> audit
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
                v = re.split(r"\s+#", v, 1)[0].strip().strip('"').strip("'")   # drop inline "# comment"
                if v: os.environ.setdefault(k.strip(), v)

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
            r = fn(*a, **k)
            if r == "skip": return  # optional step turned off: not marked done, so a later run can enable it
            if r is False: log(name, "not complete - will retry on next run"); return
            st["done"].append(name); save_state(st)
        return run
    return deco

# ---------------------------------------------------------------- shared steps (launch + connect)
GA_ADMIN = "https://analyticsadmin.googleapis.com/v1beta"

def do_zone(st, d):
    z = poll(lambda: cf_zone(d), "Cloudflare zone", every=20, timeout=900)
    st["zone_id"] = z["id"]; log("zone", f"zone {z['id']} status={z.get('status')}")

def do_gsc(st, d, put_txt):
    """Verify the domain with a DNS TXT record and add the Search Console domain property.
    put_txt(zone_id, name, token) writes the record and returns a short status word."""
    site = {"type": "INET_DOMAIN", "identifier": d}
    tok = g("POST", "https://www.googleapis.com/siteVerification/v1/token",
            {"site": site, "verificationMethod": "DNS_TXT"})["token"]
    log("gsc", f"TXT {d}: {put_txt(st['zone_id'], d, tok)}")
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

def do_ga4(st, d, canon, tz):
    """Create the GA4 property (once) and a web stream; record both in state. Returns the measurement ID."""
    if "ga4_property" not in st:  # saved immediately so a re-run never creates a duplicate property
        prop = g("POST", f"{GA_ADMIN}/properties", {"parent": f"accounts/{env('GA4_ACCOUNT_ID')}", "displayName": d,
                                                    "timeZone": tz, "currencyCode": "USD"})
        st["ga4_property"] = prop["name"]; save_state(st)
    stream = g("POST", f"{GA_ADMIN}/{st['ga4_property']}/dataStreams", {"type": "WEB_DATA_STREAM", "displayName": d,
                                                                       "webStreamData": {"defaultUri": f"https://{canon}"}})
    mid = stream["webStreamData"]["measurementId"]; st["ga4"] = {"property": st["ga4_property"], "measurement_id": mid}
    return mid

def sitemap_urls(sm, expand_index):
    xml = fetch(sm)[2]
    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)
    if expand_index and "<sitemapindex" in xml:  # one level deep: page URLs, not child sitemap URLs
        locs = [u for child in locs for u in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", fetch(child)[2])]
    return locs[:10000]

def do_sitemap(d, canon, sm, expand_index):
    if fetch(sm)[0] != 200: log("sitemap", f"{sm} not found - add one to the repo, then re-run"); return False
    g("PUT", f"https://www.googleapis.com/webmasters/v3/sites/{gsc_site(d)}/sitemaps/{urllib.parse.quote(sm, safe='')}")
    log("sitemap", f"submitted {sm} to Search Console")
    key = os.environ.get("INDEXNOW_KEY")
    if key and fetch(f"https://{canon}/{key}.txt")[0] == 200:
        urls = sitemap_urls(sm, expand_index)
        try:
            http("POST", "https://api.indexnow.org/indexnow", body={"host": canon, "key": key, "urlList": urls})
            log("sitemap", f"IndexNow (Bing, Yandex, ...) pinged with {len(urls)} URLs")
        except HTTPError as e: log("sitemap", f"IndexNow failed: {e}")
    else: log("sitemap", "IndexNow skipped (INDEXNOW_KEY unset or /<key>.txt not served)")

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
    def s_zone(): do_zone(st, d)

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
        do_gsc(st, d, lambda z, name, tok: cf_upsert(z, 'TXT', name, tok, match='google-site-verification'))

    @step(st, "ga4")
    def s_ga4():
        if a.no_ga: log("ga4", "skipped (--no-ga)"); return "skip"
        mid = do_ga4(st, d, canon, a.tz)
        vc("POST", f"/v10/projects/{st['vercel_project']}/env?upsert=true",
           {"key": a.ga_env, "value": mid, "type": "plain", "target": ["production", "preview"]})
        log("ga4", f"{st['ga4_property']} stream {mid} -> Vercel env {a.ga_env}")

    @step(st, "posthog")
    def s_posthog():
        if not (a.posthog or a.posthog_new): return "skip"
        host = os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com")  # ingestion host; EU: https://eu.i.posthog.com
        if a.posthog_new:  # separate project per product - free PostHog orgs allow only one project
            if "posthog_key" not in st:
                app = host.replace(".i.posthog.com", ".posthog.com")
                r = http("POST", f"{app}/api/organizations/{env('POSTHOG_ORG_ID')}/projects/", env("POSTHOG_PERSONAL_API_KEY"), {"name": d})
                st["posthog_key"] = r["api_token"]; st["posthog_project"] = r.get("id"); save_state(st)
            key = st["posthog_key"]; how = f"new project {st.get('posthog_project')}"
        else:  # shared project: split products in PostHog by the $host property
            key = env("POSTHOG_PROJECT_KEY"); how = "shared project (filter by $host)"
        for k, v in (("NEXT_PUBLIC_POSTHOG_KEY", key), ("NEXT_PUBLIC_POSTHOG_HOST", host)):
            vc("POST", f"/v10/projects/{st['vercel_project']}/env?upsert=true",
               {"key": k, "value": v, "type": "plain", "target": ["production", "preview"]})
        log("posthog", f"{how} -> Vercel env NEXT_PUBLIC_POSTHOG_KEY / _HOST")
        if "deploy" in st["done"]: log("posthog", "site already deployed - push a commit (or redeploy) so the new env vars take effect")

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
    def s_sitemap(): return do_sitemap(d, canon, f"https://{canon}{a.sitemap}", expand_index=False)

    for s in (s_domain, s_zone, s_vercel, s_dns, s_gsc, s_ga4, s_posthog, s_deploy, s_live, s_sitemap): s()
    print(); fails = run_audit(f"https://{canon}", quiet=False)
    print(f"\nDone. State: state/{d}.json. {'Fix the FAIL lines above.' if fails else 'All critical checks passed.'}")
    print("One-time manual: in Bing Webmaster Tools, 'Import from Google Search Console' (covers all future sites).")

# ---------------------------------------------------------------- connect (site already live on any host)
def cf_add_txt(zone_id, name, content):
    """Add a TXT record only if that exact value isn't there yet. Never edits or deletes other records."""
    recs = cf("GET", f"/zones/{zone_id}/dns_records?type=TXT&name={name}") or []
    if any(r["content"].strip('"') == content for r in recs): return "unchanged"
    cf("POST", f"/zones/{zone_id}/dns_records", {"type": "TXT", "name": name, "content": content, "ttl": 1})
    return "created"

def gsc_owned(d):
    try: sites = g("GET", "https://www.googleapis.com/webmasters/v3/sites").get("siteEntry", [])
    except HTTPError: return False
    return any(s.get("siteUrl") == f"sc-domain:{d}" and s.get("permissionLevel") == "siteOwner" for s in sites)

def ga4_find(d, canon):
    """Existing GA4 property named after the domain, and its web stream for the canonical host, if any."""
    try:
        props = g("GET", f"{GA_ADMIN}/properties?filter=parent:accounts/{env('GA4_ACCOUNT_ID')}&pageSize=200").get("properties", [])
    except HTTPError: return None, None
    prop = next((p for p in props if p.get("displayName") in (d, canon)), None)
    if not prop: return None, None
    try: streams = g("GET", f"{GA_ADMIN}/{prop['name']}/dataStreams").get("dataStreams", [])
    except HTTPError: streams = []
    host = lambda s: urllib.parse.urlparse((s.get("webStreamData") or {}).get("defaultUri", "")).netloc
    stream = next((s for s in streams if s.get("type") == "WEB_DATA_STREAM" and host(s) in (d, canon)), None)
    return prop["name"], (stream["webStreamData"]["measurementId"] if stream else None)

def find_sitemap(canon, path):
    if path: return f"https://{canon}{path}"
    s, _, robots, _ = fetch(f"https://{canon}/robots.txt")
    listed = re.findall(r"(?im)^sitemap:\s*(\S+)", robots) if s == 200 else []
    for sm in listed + [f"https://{canon}/sitemap.xml", f"https://{canon}/sitemap-index.xml"]:
        if fetch(sm)[0] == 200: return sm
    return f"https://{canon}/sitemap.xml"

def cmd_connect(a):
    """Search Console + GA4 + sitemap for a domain that already serves a site on any host. No hosting or Vercel calls."""
    d = a.domain.lower().strip().removeprefix("https://").removeprefix("http://").strip("/")
    d = d.removeprefix("www.")
    canon = f"www.{d}" if a.canonical == "www" else d
    connect_accounts(want_ga=not a.no_ga, want_vercel=False)
    st = load_state(d)

    # read-only discovery, so the plan can say exactly what will change
    print(f"\nConnect {d}  (canonical https://{canon})\n")
    status, hdr, _, _ = fetch(f"https://{canon}/")
    if status != 200: die(f"https://{canon}/ returned {status} - connect is for sites that are already live")
    server = {k.lower(): v for k, v in hdr.items()}.get("server", "unknown")
    z = cf_zone(d)
    if not z: die(f"no Cloudflare zone for {d} in this account - connect needs it to add the Google verification TXT record")
    owned = "gsc" in st["done"] or gsc_owned(d)
    vtxt = [r for r in cf("GET", f"/zones/{z['id']}/dns_records?type=TXT&name={d}") or []
            if r["content"].strip('"').startswith("google-site-verification")]
    prop, mid = (None, None) if a.no_ga or "ga4" in st["done"] else ga4_find(d, canon)
    sm = find_sitemap(canon, a.sitemap)

    def line(n, name, text): print(f"   {n}. {name:<14} {'already done' if name in st['done'] else text}")
    print(f"  Live now: https://{canon}/ -> 200 (server: {server}). Hosting and DNS routing are left exactly as they are.\n")
    print("  Plan:")
    line(1, "zone", f"use Cloudflare zone {z['id']} (read only)")
    line(2, "gsc", "already verified in Search Console - no DNS change" if owned else
         f"add ONE TXT record on {d}: google-site-verification=<token> ({len(vtxt)} existing verification record(s) kept as is), "
         f"then add Search Console property sc-domain:{d}")
    if a.no_ga: line(3, "ga4", "skipped (--no-ga)")
    elif prop and mid: line(3, "ga4", f"reuse existing property {prop} and web stream {mid}")
    elif prop: line(3, "ga4", f"reuse existing property {prop}, create a web stream for https://{canon}")
    else: line(3, "ga4", f"create GA4 property '{d}' (time zone {a.tz}) and a web stream for https://{canon}")
    line(4, "sitemap", f"submit {sm} to Search Console; IndexNow ping if INDEXNOW_KEY is set and /<key>.txt is served")
    print("   5. audit          read-only checks of the live site")
    print("\n  Nothing else is touched: no A/AAAA/CNAME/MX/SPF/DMARC or other TXT records, no hosting, no Vercel.")
    if input("\n  Proceed? Type y to continue: ").strip().lower() != "y": print("  Cancelled - nothing was changed."); return

    st.update(mode="connect", canonical=canon); save_state(st)

    @step(st, "zone")
    def s_zone(): do_zone(st, d)

    @step(st, "gsc")
    def s_gsc():
        if gsc_owned(d): log("gsc", f"sc-domain:{d} already verified - no DNS change"); return
        do_gsc(st, d, cf_add_txt)

    @step(st, "ga4")
    def s_ga4():
        if a.no_ga: log("ga4", "skipped (--no-ga)"); return "skip"
        if prop and "ga4_property" not in st: st["ga4_property"] = prop; save_state(st)
        if mid: st["ga4"] = {"property": st["ga4_property"], "measurement_id": mid}; m = mid
        else: m = do_ga4(st, d, canon, a.tz)
        save_state(st)
        log("ga4", f"{st['ga4_property']} measurement ID {m} (saved to state/{d}.json)")

    @step(st, "sitemap")
    def s_sitemap(): return do_sitemap(d, canon, sm, expand_index=True)

    for s in (s_zone, s_gsc, s_ga4, s_sitemap): s()
    print(); fails = run_audit(f"https://{canon}", quiet=False)
    if st.get("ga4"): print(f"\n  GA4 measurement ID: {st['ga4']['measurement_id']}  (put it in your site's analytics config)")
    print(f"\nDone. State: state/{d}.json. {'Fix the FAIL lines above.' if fails else 'All critical checks passed.'}")

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
def print_domains(rows, show_unavailable=True):
    """Numbered table. Returns the available domains in the order shown, so a number picks one."""
    avail = [r for r in rows if r.get("registrable")]
    taken = [r for r in rows if not r.get("registrable")]
    print(f"\n   #  {'domain':<32} {'1st yr':>8} {'renew':>8}")
    for i, r in enumerate(avail, 1):
        p = r.get("pricing") or {}
        print(f"  {i:>2}  {r.get('name',''):<32} {str(p.get('registration_cost','-')):>8} {str(p.get('renewal_cost','-')):>8}")
    if show_unavailable:
        for r in taken: print(f"   -  {r.get('name',''):<32} {'taken':>8}   {r.get('reason','')}")
    if not avail: print("  (none available)")
    return [(r["name"], (r.get("pricing") or {})) for r in avail]

def check_names(names, tlds="com,io,app,co"):
    tl = [t.strip(".") for t in tlds.split(",")]
    full = [n.lower() if "." in n else f"{n.lower()}.{t}" for n in names for t in ([None] if "." in n else tl)]
    return print_domains(sorted(cf_check(full), key=lambda x: x.get("name", "")))

def suggest_names(keywords, limit=20):
    q = urllib.parse.quote(" ".join(keywords))
    r = cf("GET", f"/accounts/{env('CF_ACCOUNT_ID')}/registrar/domain-search?q={q}&limit={limit}")
    rows = (r.get("domains") if isinstance(r, dict) else r) or []
    print(f"\n  Available names for: {' '.join(keywords)}")
    return print_domains(rows, show_unavailable=False)

def cmd_check(a):
    check_names(a.names, a.tlds); print("\n  Next: python3 launch.py launch <domain> --repo <owner/repo> --buy   (or run: python3 launch.py menu)\n")

def cmd_suggest(a):
    suggest_names(a.keywords, a.limit); print("\n  Next: python3 launch.py launch <domain> --repo <owner/repo> --buy   (or run: python3 launch.py menu)\n")

# ---------------------------------------------------------------- interactive menu
def ask(q, default=""):
    v = input(f"  {q}" + (f" [{default}]" if default else "") + ": ").strip()
    return v or default

def yes(q, default=True):
    v = ask(q + (" (Y/n)" if default else " (y/N)")).lower()
    return default if not v else v.startswith("y")

def pick_and_launch(options):
    if not options: return
    v = ask("Type a number to launch that domain, or press Enter to go back")
    if v.isdigit() and 1 <= int(v) <= len(options): launch_flow(options[int(v) - 1][0])

# ---------------------------------------------------------------- guided account connection
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/siteverification", "https://www.googleapis.com/auth/webmasters",
                 "https://www.googleapis.com/auth/analytics.edit"]

def set_env(key, value):
    f = HERE / ".env"
    lines = f.read_text().splitlines() if f.exists() else []
    out, done = [], False
    for ln in lines:
        if re.match(rf"^\s*{re.escape(key)}\s*=", ln): out.append(f"{key}={value}"); done = True
        else: out.append(ln)
    if not done: out.append(f"{key}={value}")
    f.write_text("\n".join(out) + "\n"); os.environ[key] = value

def secret(q):
    import getpass
    return getpass.getpass(f"  {q} (hidden while you paste): ").strip()

def open_page(url, what):
    import webbrowser
    print(f"\n  Opening {what}:\n  {url}")
    try: webbrowser.open(url)
    except Exception: pass

def connect_cloudflare():
    print("\n  -- Cloudflare (buys the domain and sets DNS) --")
    open_page("https://dash.cloudflare.com/profile/api-tokens", "Cloudflare API tokens")
    print("  Click 'Create Token' > 'Create Custom Token', name it launchkit, and add these permissions:\n"
          "    Account > Registrar (domains) > Edit\n    Zone > Zone > Read\n    Zone > DNS > Edit\n"
          "  Zone resources: All zones. Then 'Continue to summary' > 'Create Token' and copy it.")
    while True:
        tok = secret("Paste the Cloudflare token")
        try:
            r = http("GET", CF + "/user/tokens/verify", tok)
            if r.get("result", {}).get("status") == "active": break
        except HTTPError: pass
        print("  That token didn't verify - try copying it again.")
    set_env("CF_API_TOKEN", tok)
    accts = []
    try: accts = http("GET", CF + "/accounts", tok).get("result") or []
    except HTTPError: pass
    if len(accts) == 1: acct = accts[0]["id"]
    else:
        for i, a_ in enumerate(accts, 1): print(f"   {i}) {a_['name']}  {a_['id']}")
        v = ask("Account number from the list, or paste your Account ID (it's in the dashboard URL after dash.cloudflare.com/)")
        acct = accts[int(v) - 1]["id"] if v.isdigit() and 1 <= int(v) <= len(accts) else v
    set_env("CF_ACCOUNT_ID", acct)
    print("  Cloudflare connected. To buy domains through it you also need, once, in the dashboard under Domains > Registrar:\n"
          "  a payment method, a default contact, and the registration agreement accepted.")

def connect_vercel():
    print("\n  -- Vercel (hosting) --")
    open_page("https://vercel.com/account/tokens", "Vercel tokens")
    print("  Create a token (name: launchkit, scope: your account/team, no expiry or long expiry) and copy it.")
    while True:
        tok = secret("Paste the Vercel token")
        try: user = http("GET", "https://api.vercel.com/v2/user", tok).get("user", {}); break
        except HTTPError: print("  That token didn't work - try again.")
    set_env("VERCEL_TOKEN", tok)
    if user.get("defaultTeamId"): set_env("VERCEL_TEAM_ID", user["defaultTeamId"])
    print(f"  Vercel connected as {user.get('username') or user.get('email')}.")
    open_page("https://github.com/apps/vercel/installations/new", "the Vercel GitHub app")
    print("  Make sure Vercel can see your GitHub repos (choose 'All repositories'). Skip if already done.")
    ask("Press Enter when done")

def google_login():
    import http.server as _hs, secrets as _s, socket, webbrowser
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
    redirect, state, got = f"http://127.0.0.1:{port}", _s.token_urlsafe(16), {}
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": env("GOOGLE_CLIENT_ID"), "redirect_uri": redirect, "response_type": "code",
        "scope": " ".join(GOOGLE_SCOPES), "access_type": "offline", "prompt": "consent", "state": state})
    class H(_hs.BaseHTTPRequestHandler):
        def do_GET(self):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if q.get("state") == state: got.update(q)
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
            self.wfile.write(b"<h2>launchkit: Google connected. You can close this tab.</h2>")
        def log_message(self, *a): pass
    srv = _hs.HTTPServer(("127.0.0.1", port), H); srv.timeout = 300
    print("\n  A Google sign-in page is opening. Choose your account and allow access.\n"
          "  If Google says the app isn't verified: click 'Advanced' > 'Go to launchkit' (it's your own app).")
    webbrowser.open(url); print(f"  (If it didn't open: {url})")
    t0 = time.time()
    while not got and time.time() - t0 < 300: srv.handle_request()
    srv.server_close()
    if "code" not in got: die(f"Google sign-in didn't complete: {got.get('error', 'timed out')}")
    r = http("POST", "https://oauth2.googleapis.com/token", form={
        "code": got["code"], "client_id": env("GOOGLE_CLIENT_ID"), "client_secret": env("GOOGLE_CLIENT_SECRET"),
        "redirect_uri": redirect, "grant_type": "authorization_code"})
    if not r.get("refresh_token"): die("Google didn't return a refresh token - run Connect Google again.")
    set_env("GOOGLE_REFRESH_TOKEN", r["refresh_token"]); _gtok.clear()
    print("  Google connected.")

def connect_google_client():
    print("\n  -- Google (Search Console + Analytics) - one-time app setup, ~5 minutes --")
    steps = [("https://console.cloud.google.com/projectcreate", "1) Create a project called launchkit (then make sure it's selected at the top)"),
             ("https://console.cloud.google.com/apis/library/siteverification.googleapis.com", "2) Click Enable"),
             ("https://console.cloud.google.com/apis/library/searchconsole.googleapis.com", "3) Click Enable"),
             ("https://console.cloud.google.com/apis/library/analyticsadmin.googleapis.com", "4) Click Enable"),
             ("https://console.cloud.google.com/auth/overview", "5) Get started: app name launchkit, your email, Audience: External, finish"),
             ("https://console.cloud.google.com/auth/audience", "6) Click 'Publish app' (otherwise Google logs you out every 7 days)"),
             ("https://console.cloud.google.com/auth/clients/create", "7) Application type: Desktop app, name launchkit, Create - keep the popup open")]
    for url, text in steps:
        open_page(url, text.split(") ", 1)[1].split(" (")[0]); print(f"  {text}"); ask("Press Enter when done")
    set_env("GOOGLE_CLIENT_ID", ask("Paste the Client ID"))
    set_env("GOOGLE_CLIENT_SECRET", secret("Paste the Client secret"))

def connect_ga4():
    try: accts = g("GET", "https://analyticsadmin.googleapis.com/v1beta/accounts").get("accounts", [])
    except HTTPError: accts = []
    if not accts:
        open_page("https://analytics.google.com/analytics/web/#/provision", "Google Analytics")
        print("  You don't have a Google Analytics account yet. Create one there (account name is enough; you can skip the property).")
        ask("Press Enter when done")
        try: accts = g("GET", "https://analyticsadmin.googleapis.com/v1beta/accounts").get("accounts", [])
        except HTTPError: accts = []
    if not accts: print("  Still no Analytics account - launching without Google Analytics this time."); return False
    pick = accts[0]
    if len(accts) > 1:
        for i, a_ in enumerate(accts, 1): print(f"   {i}) {a_.get('displayName')}")
        v = ask("Which Analytics account", "1"); pick = accts[int(v) - 1] if v.isdigit() and 1 <= int(v) <= len(accts) else accts[0]
    set_env("GA4_ACCOUNT_ID", pick["name"].split("/")[-1]); return True

def connect_accounts(want_ga=True, want_posthog=False, want_vercel=True):
    """Ask for whatever access is missing. Each piece is asked once and saved to .env."""
    if not (HERE / ".env").exists() and (HERE / ".env.example").exists(): (HERE / ".env").write_text((HERE / ".env.example").read_text())
    load_env()
    if not (os.environ.get("CF_API_TOKEN") and os.environ.get("CF_ACCOUNT_ID")): connect_cloudflare()
    if want_vercel and not os.environ.get("VERCEL_TOKEN"): connect_vercel()
    if not (os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET")): connect_google_client()
    if not os.environ.get("GOOGLE_REFRESH_TOKEN"): google_login()
    ga_ok = True
    if want_ga and not os.environ.get("GA4_ACCOUNT_ID"): ga_ok = connect_ga4()
    if want_posthog and not os.environ.get("POSTHOG_PROJECT_KEY"):
        open_page("https://us.posthog.com/settings/project", "PostHog project settings")
        set_env("POSTHOG_PROJECT_KEY", ask("Paste the Project API key (starts with phc_)"))
    return ga_ok

def launch_flow(domain):
    domain = domain.lower().strip()
    print(f"\n  Launching {domain}\n")
    repo = ask("GitHub repo for this app (owner/repo)")
    if "/" not in repo: print("  Need it as owner/repo, e.g. SupratikSarkar88/myapp"); return
    nextjs = yes("Is it a Next.js app?"); ga = yes("Set up Google Analytics?")
    ph = yes("Set up PostHog (user sessions / replay)?", False); mail = yes("Will this domain send email?", False)
    if not connect_accounts(want_ga=ga, want_posthog=ph): ga = False
    ns = argparse.Namespace(domain=domain, repo=repo, buy=False, canonical="www", sitemap="/sitemap.xml",
                            ga_env="NEXT_PUBLIC_GA_ID", tz="Asia/Kolkata", posthog_new=False,
                            framework="nextjs" if nextjs else "", no_ga=not ga, posthog=ph, email=mail)
    print()
    if not cf_zone(domain):
        info = (cf_check([domain]) or [{}])[0]
        if not info.get("registrable"): print(f"\n  {domain} can't be bought: {info.get('reason', 'not available')}"); return
        p = info.get("pricing") or {}
        print(f"\n  {domain} costs {p.get('registration_cost')} {p.get('currency', 'USD')} for the first year "
              f"(renews at {p.get('renewal_cost')}), charged to your Cloudflare payment method.")
        if ask("Type BUY to purchase it and launch (anything else cancels)") != "BUY": print("  Cancelled - nothing was bought."); return
        ns.buy = True
    else:
        print("  You already own this domain in Cloudflare - no purchase needed.")
    cmd_launch(ns)

def cmd_menu(_a=None):
    items = [("Suggest domain names from keywords", "suggest"), ("Check names I already have", "check"),
             ("Launch with a domain I've chosen", "launch"),
             ("Already live somewhere else? Connect Search Console + GA4", "connect"), ("Audit a live site", "audit"),
             ("Search Console report", "report"), ("Connect accounts / reconnect Google", "google")]
    while True:
        print("\n  ==========  launchkit  ==========")
        for i, (label, _) in enumerate(items, 1): print(f"   {i}) {label}")
        print("   q) Quit")
        c = ask("Choose").lower()
        if c in ("q", "quit", "exit"): return
        if not c.isdigit() or not 1 <= int(c) <= len(items): continue
        action = items[int(c) - 1][1]
        try:
            if action == "suggest": pick_and_launch(suggest_names(ask("Keywords (e.g. invoice tool)").split()))
            elif action == "check": pick_and_launch(check_names(ask("Names, space separated (e.g. tinyinvoice quickbill.io)").split()))
            elif action == "launch": launch_flow(ask("Domain (e.g. myapp.com)"))
            elif action == "connect":
                dom = ask("Domain (e.g. myapp.com)")
                canon = "www" if yes("Is the www address the main one (www.example.com)?", False) else "apex"
                cmd_connect(argparse.Namespace(domain=dom, canonical=canon, no_ga=not yes("Set up Google Analytics?"),
                                               tz="Asia/Kolkata", sitemap=""))
            elif action == "audit":
                u = ask("Site (e.g. myapp.com)"); u = u if u.startswith("http") else f"https://{u}"
                print(f"\n  Audit {u}\n"); run_audit(u)
            elif action == "report": cmd_report(argparse.Namespace(domain=ask("Domain"), days=28))
            elif action == "google":
                connect_accounts(want_ga=False) if not os.environ.get("GOOGLE_CLIENT_ID") else google_login()
        except SystemExit: pass          # die() prints the error; stay in the menu
        except KeyboardInterrupt: print("\n  Stopped.")
        except Exception as e: print(f"\n  ERROR: {e}")
        input("\n  Press Enter for the menu...")

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
    sg = sub.add_parser("suggest"); sg.add_argument("keywords", nargs="+"); sg.add_argument("--limit", type=int, default=20)
    c = sub.add_parser("check"); c.add_argument("names", nargs="+"); c.add_argument("--tlds", default="com,io,app,co")
    l = sub.add_parser("launch"); l.add_argument("domain"); l.add_argument("--repo", required=True, help="GitHub owner/repo")
    l.add_argument("--buy", action="store_true", help="allow purchasing the domain")
    l.add_argument("--canonical", choices=["www", "apex"], default="www")
    l.add_argument("--framework", default="nextjs", help="Vercel framework preset; '' for auto/other")
    l.add_argument("--sitemap", default="/sitemap.xml"); l.add_argument("--no-ga", action="store_true")
    l.add_argument("--ga-env", default="NEXT_PUBLIC_GA_ID", help="env var name your code reads the GA4 ID from")
    l.add_argument("--tz", default="Asia/Kolkata", help="GA4 reporting time zone")
    l.add_argument("--posthog", action="store_true", help="wire PostHog using the shared project key in .env")
    l.add_argument("--posthog-new", action="store_true", help="create a separate PostHog project (needs a paid PostHog org)")
    l.add_argument("--email", action="store_true", help="domain will send mail: skip SPF -all / DMARC reject")
    cn = sub.add_parser("connect", help="site already live on any host: Search Console + GA4 + sitemap, no hosting changes")
    cn.add_argument("domain"); cn.add_argument("--canonical", choices=["www", "apex"], default="apex")
    cn.add_argument("--no-ga", action="store_true"); cn.add_argument("--tz", default="Asia/Kolkata", help="GA4 reporting time zone")
    cn.add_argument("--sitemap", default="", help="sitemap path; default: robots.txt Sitemap line, then /sitemap.xml, /sitemap-index.xml")
    au = sub.add_parser("audit"); au.add_argument("url")
    rp = sub.add_parser("report"); rp.add_argument("domain"); rp.add_argument("--days", type=int, default=28)
    sub.add_parser("menu"); sub.add_parser("setup")
    if len(sys.argv) == 1: sys.argv.append("menu")   # no arguments = interactive menu
    if len(sys.argv) == 2 and "." in sys.argv[1] and not sys.argv[1].startswith("-"):   # launch.py FireEnough.com
        try: launch_flow(sys.argv[1])
        except KeyboardInterrupt: print("\n  Stopped. Run the same command again to continue.")
        return
    a = ap.parse_args()
    if a.cmd == "audit":
        u = a.url if a.url.startswith("http") else f"https://{a.url}"
        print(f"\nAudit {u}\n"); sys.exit(1 if run_audit(u) else 0)
    {"suggest": cmd_suggest, "check": cmd_check, "launch": cmd_launch, "connect": cmd_connect, "report": cmd_report, "menu": cmd_menu,
     "setup": lambda _a: connect_accounts()}[a.cmd](a)

if __name__ == "__main__":
    main()
