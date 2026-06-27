#!/usr/bin/env python3
"""
Senate STOCK Act PTR scraper.
Runs on GitHub Actions (AWS us-east-1 runners bypass Akamai geo-block on disclosure.senate.gov).

Outputs data/senate_ptrs.json — rolling 90-day window of Senate stock trades.
The congress-tracker bot reads this file via raw.githubusercontent.com.
"""

import io
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pdfplumber
import requests
from bs4 import BeautifulSoup

BASE = "https://www.disclosure.senate.gov"
# Discovered from main.js on disclosure.senate.gov — the actual eFD search domain
EFDSEARCH = "https://efdsearch.senate.gov"
EFD = "https://efd.senate.gov"
OUTPUT = Path("data/senate_ptrs.json")
LOOKBACK_DAYS = 90
MAX_PDFS = 60

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def log(msg):
    print(f"[{datetime.utcnow():%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# PDF parsing — Senate PTRs follow the same STOCK Act table format as House
# ---------------------------------------------------------------------------

def _amount_lower(s: str) -> int:
    nums = re.findall(r"\d+", s.replace(",", "").replace("\n", " "))
    return int(nums[0]) if nums else 0


def _normalize_type(raw: str) -> str:
    if not raw:
        return "Unknown"
    lower = raw.strip().lower()
    if "partial" in lower:
        return "Sell (Partial)"
    if lower.startswith("p") or lower == "purchase":
        return "Buy"
    if lower.startswith("s") or lower == "sale":
        return "Sell"
    if lower.startswith("e"):
        return "Exchange"
    return raw.strip().title()


def _extract_ticker(text: str) -> str:
    m = re.search(r"\(([A-Z]{1,5})\)\s*\[(ST|OP|WT)\]", text)
    return m.group(1) if m else "N/A"


def _clean_asset(text: str) -> str:
    return re.sub(r"\s*\([A-Z0-9\.]{1,9}\)\s*\[[A-Z]+\].*", "", text).replace("\n", " ").strip()


def parse_ptr_pdf(pdf_bytes: bytes, member_name: str, filing_date: str = "") -> list:
    trades = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                for table in page.extract_tables():
                    in_data = False
                    for row in table:
                        if not row or not any(row):
                            continue
                        row_text = " ".join(str(c) for c in row if c)

                        if "Transaction" in row_text and "Amount" in row_text:
                            in_data = True
                            continue
                        if not in_data:
                            continue
                        if any(k in row_text for k in ("Filing Status", "Subholding", "CERTIFY")):
                            continue

                        # Format A: cells properly split
                        if (
                            len(row) >= 7
                            and row[3]
                            and str(row[3]).strip() in ("S", "P", "E", "S (Partial)", "Exchange", "Sale", "Purchase")
                        ):
                            owner = str(row[1] or "").strip() or "Member"
                            asset_raw = str(row[2] or "").replace("\x00", "").strip()
                            date_str = str(row[4] or "").strip()
                            amount_raw = str(row[6] or "").replace("\n", " ").strip()
                            try:
                                tx_date = datetime.strptime(date_str, "%m/%d/%Y")
                            except ValueError:
                                continue
                            trades.append({
                                "member": member_name, "chamber": "Senate",
                                "owner": owner,
                                "asset": _clean_asset(asset_raw),
                                "ticker": _extract_ticker(asset_raw),
                                "type": _normalize_type(str(row[3])),
                                "date": tx_date.strftime("%Y-%m-%d"),
                                "filing_date": filing_date,
                                "amount_raw": amount_raw,
                                "amount_lower": _amount_lower(amount_raw),
                            })

                        # Format B: everything merged into first cell
                        elif row[1] is None and row[0]:
                            cell = str(row[0]).replace("\x00", " ")
                            m = re.search(
                                r"(SP|JT|DC)?\s*(.*?)\s+(S|P|E)\s+(\d{2}/\d{2}/\d{4})\s+\d{2}/\d{2}/\d{4}",
                                cell, re.DOTALL,
                            )
                            amount_m = re.search(
                                r"\$([\d,]+)\s*-\s*\n?(?:\([A-Z0-9\.]+\)\s*\[[A-Z]+\]\s*)?\$([\d,]+)", cell,
                            )
                            if not m or not amount_m:
                                continue
                            try:
                                tx_date = datetime.strptime(m.group(4), "%m/%d/%Y")
                            except ValueError:
                                continue
                            amount_raw = f"${amount_m.group(1)} - ${amount_m.group(2)}"
                            trades.append({
                                "member": member_name, "chamber": "Senate",
                                "owner": m.group(1) or "Member",
                                "asset": _clean_asset(m.group(2).replace("\n", " ").strip()),
                                "ticker": _extract_ticker(cell),
                                "type": _normalize_type(m.group(3)),
                                "date": tx_date.strftime("%Y-%m-%d"),
                                "filing_date": filing_date,
                                "amount_raw": amount_raw,
                                "amount_lower": int(amount_m.group(1).replace(",", "")),
                            })
    except Exception as e:
        log(f"  PDF parse error for {member_name}: {e}")
    return trades


# ---------------------------------------------------------------------------
# Senate eFD site discovery + filing search
# ---------------------------------------------------------------------------

def _extract_filings_from_json(data) -> list:
    """Parse common JSON response formats from government disclosure APIs."""
    filings = []

    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        # Elasticsearch: {"hits": {"hits": [...]}}
        if "hits" in data:
            hits = data["hits"]
            items = hits.get("hits", hits) if isinstance(hits, dict) else hits
            items = [h.get("_source", h) for h in items]
        elif "results" in data:
            items = data["results"]
        elif "data" in data:
            items = data["data"]
        elif "filings" in data:
            items = data["filings"]
        else:
            log(f"    Unknown JSON structure, keys: {list(data.keys())}")
            return []
    else:
        return []

    for item in items:
        if not isinstance(item, dict):
            continue
        member = (
            item.get("name") or item.get("filer_name")
            or (item.get("first_name", "") + " " + item.get("last_name", "")).strip()
            or "Unknown"
        ).strip()
        pdf_url = (
            item.get("pdf_url") or item.get("document_url") or item.get("link")
            or item.get("url") or item.get("filing_url") or ""
        )
        filing_date = item.get("date_filed") or item.get("date") or item.get("filed_at") or ""
        if member or pdf_url:
            filings.append({"member": member, "pdf_url": pdf_url, "filing_date": filing_date, "raw": item})

    log(f"    Extracted {len(filings)} filings from JSON")
    return filings


def _find_ptr_links_in_html(soup: BeautifulSoup, base_domain: str) -> list:
    """
    Find PTR PDF links in an HTML page.
    Strict: only match links that look like actual eFD financial disclosure filings,
    NOT general Senate website links (which may mention 'periodic' in an LDA context).
    """
    links = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        href_lower = href.lower()
        text = a.get_text(strip=True)

        # Must be a PDF
        if not href_lower.endswith(".pdf"):
            continue

        # Must look like an eFD filing — not a lobbying/LDA doc
        is_efd = any(seg in href_lower for seg in (
            "disclosure.senate.gov",
            "/financial/",
            "/efd/",
            "/ptr/",
            "getreport",
            "viewdocument",
            "reportid",
            "docid",
            "filer",
        ))
        if not is_efd:
            continue

        full = href if href.startswith("http") else base_domain + href
        links.append({"member": text or "Unknown", "pdf_url": full, "filing_date": ""})
    return links


def _probe_scripts_for_api(session: requests.Session, soup: BeautifulSoup, base_domain: str):
    """Fetch external JS files and look for API URL hints."""
    interesting = []
    for script in soup.find_all("script", src=True):
        src = script.get("src", "")
        if not src:
            continue
        full_src = src if src.startswith("http") else base_domain + src
        log(f"  Script src: {src}")
        # Only fetch scripts that might contain API config
        if any(kw in src.lower() for kw in ("app", "main", "bundle", "api", "config", "efd", "search")):
            try:
                r = session.get(full_src, timeout=10)
                if r.status_code == 200:
                    content = r.text
                    # Look for API URL patterns
                    urls = re.findall(r'["\'](https?://[^"\']*(?:api|search|efts|efd)[^"\']*)["\']', content)
                    for u in urls[:10]:
                        log(f"    ** API URL in script: {u}")
                        interesting.append(u)
            except Exception as e:
                log(f"    Script fetch error: {e}")
    return interesting


def _accept_agreement(session: requests.Session) -> str:
    """
    efdsearch.senate.gov gates searches behind a prohibition_agreement POST form.
    Accept it so the session cookie allows access to /search/report/data/.
    Returns the CSRF token from cookie (needed as X-CSRFToken header for AJAX calls).
    """
    try:
        r = session.get(EFDSEARCH + "/", timeout=15)
        if r.status_code != 200:
            log(f"  Agreement page returned {r.status_code}")
            return ""
        soup = BeautifulSoup(r.content, "html.parser")
        # Get CSRF from form field
        form_csrf = ""
        for inp in soup.find_all("input"):
            if inp.get("name") == "csrfmiddlewaretoken":
                form_csrf = inp.get("value", "")
                break
        # Also log cookies we have so far
        cookie_csrf = session.cookies.get("csrftoken", "")
        log(f"  Form CSRF: {form_csrf[:20]}...  Cookie CSRF: {cookie_csrf[:20]}...")

        post_resp = session.post(
            EFDSEARCH + "/",
            data={"prohibition_agreement": "1", "csrfmiddlewaretoken": form_csrf},
            headers={**HEADERS, "Referer": EFDSEARCH + "/", "Origin": EFDSEARCH},
            timeout=15,
            allow_redirects=True,
        )
        log(f"  Agreement POST → {post_resp.status_code}, URL: {post_resp.url}")
        log(f"  Cookies after POST: {dict(session.cookies)}")

        # CSRF token for AJAX calls comes from cookie, not form
        csrf_cookie = session.cookies.get("csrftoken", form_csrf)
        log(f"  CSRF cookie for AJAX: {csrf_cookie[:20]}...")

        s = BeautifulSoup(post_resp.content, "html.parser")
        title = s.find("title")
        log(f"  Post-agreement page title: {title.get_text(strip=True) if title else 'N/A'}")
        for form in s.find_all("form"):
            log(f"    Form action={form.get('action')!r}")
            for inp in form.find_all(["input", "select"]):
                if inp.get("name") not in ("csrfmiddlewaretoken",):
                    log(f"      {inp.name} name={inp.get('name')!r} value={str(inp.get('value',''))[:60]!r}")
        return csrf_cookie
    except Exception as e:
        log(f"  Agreement POST failed: {e}")
        return ""


def search_senate_ptrs(session: requests.Session, from_date: datetime, to_date: datetime) -> list:
    from_str = from_date.strftime("%Y-%m-%d")
    to_str = to_date.strftime("%Y-%m-%d")

    # --- Step 0: Accept agreement gate on efdsearch ---
    log("Accepting efdsearch prohibition agreement...")
    csrf_token = _accept_agreement(session)

    # --- Step 1: Probe the main eFD page ---
    for probe_url in [BASE + "/", EFDSEARCH + "/search/home/"]:
        log(f"Probing {probe_url} ...")
        try:
            resp = session.get(probe_url, timeout=30)
            log(f"  Status: {resp.status_code}  Size: {len(resp.content)} bytes  CT: {resp.headers.get('Content-Type','?')[:60]}")
            if resp.status_code != 200:
                continue

            soup = BeautifulSoup(resp.content, "html.parser")
            title = soup.find("title")
            log(f"  Page title: {title.get_text(strip=True) if title else 'N/A'}")

            for form in soup.find_all("form"):
                log(f"  Form action={form.get('action')!r} method={form.get('method','get')!r}")
                for f in form.find_all(["input", "select"]):
                    log(f"    {f.name} name={f.get('name')!r} value={str(f.get('value',''))[:80]!r}")

            all_links = [(a.get("href", ""), a.get_text(strip=True)) for a in soup.find_all("a", href=True)]
            log(f"  Links ({len(all_links)} total, showing first 60):")
            for href, text in all_links[:60]:
                log(f"    {href!r}: {text[:80]!r}")

            # Inline scripts mentioning api / efts / efd
            for s in soup.find_all("script", src=False):
                t = (s.string or "")
                if any(kw in t.lower() for kw in ("api", "efts", "efd", "search")) and len(t) < 10000:
                    log(f"  Inline script snippet: {t[:600]!r}")

            # External scripts
            discovered_apis = _probe_scripts_for_api(session, soup, BASE)
            if discovered_apis:
                log(f"  Discovered {len(discovered_apis)} API URLs from scripts")

        except Exception as e:
            log(f"  Probe failed: {e}")

    # --- Step 2: Try JSON API endpoints ---
    # efdsearch.senate.gov is a Django app (CSRF, DataTables).
    # PTR report type is commonly "11" or "ptr" in Senate eFD systems.
    api_urls = [
        # DataTables server-side: most likely endpoint pattern for Django eFD
        f"{EFDSEARCH}/search/report/data/?report_types[]=11&dateRange=custom&fromDate={from_str}&toDate={to_str}&draw=1&start=0&length=100",
        f"{EFDSEARCH}/search/report/data/?report_types[]=ptr&dateRange=custom&fromDate={from_str}&toDate={to_str}&draw=1&start=0&length=100",
        f"{EFDSEARCH}/search/report/data/?report_types[]=PTR&dateRange=custom&fromDate={from_str}&toDate={to_str}&draw=1&start=0&length=100",
        # Without DataTables params
        f"{EFDSEARCH}/search/report/data/?report_types[]=11&dateRange=custom&fromDate={from_str}&toDate={to_str}",
        f"{EFDSEARCH}/search/results/?report_types[]=11&dateRange=custom&fromDate={from_str}&toDate={to_str}",
        f"{EFDSEARCH}/search/results/?report_types[]=PTR&dateRange=custom&fromDate={from_str}&toDate={to_str}",
        # Alternate param names
        f"{EFDSEARCH}/search/report/data/?reportType=PTR&fromDate={from_str}&toDate={to_str}&draw=1&start=0&length=100",
        f"{EFDSEARCH}/api/search/?report_types=PTR&dateRange=custom&fromDate={from_str}&toDate={to_str}",
    ]

    ajax_headers = {
        **HEADERS,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{EFDSEARCH}/search/home/",
    }
    if csrf_token:
        ajax_headers["X-CSRFToken"] = csrf_token

    for url in api_urls:
        try:
            r = session.get(url, headers=ajax_headers, timeout=15)
            ct = r.headers.get("Content-Type", "")
            log(f"  {url}: {r.status_code}  CT={ct[:60]}")
            if r.status_code in (403, 503):
                log(f"    Error body: {r.text[:400]}")

            if r.status_code != 200:
                continue

            if "json" in ct:
                data = r.json()
                log(f"    JSON preview: {json.dumps(data)[:800]}")
                filings = _extract_filings_from_json(data)
                if filings:
                    log(f"    SUCCESS: {len(filings)} filings via JSON at {url}")
                    return filings
            else:
                s = BeautifulSoup(r.content, "html.parser")
                log(f"    HTML title: {(s.find('title') or s).get_text(strip=True)[:80]}")
                # Log first 30 links to understand what page this is
                for a in s.find_all("a", href=True)[:30]:
                    log(f"    Link: {a.get('href')!r}: {a.get_text(strip=True)[:60]!r}")
                # Try strict eFD-link matching
                links = _find_ptr_links_in_html(s, BASE)
                if links:
                    log(f"    SUCCESS: {len(links)} PTR links in HTML at {url}")
                    return links

        except Exception as e:
            log(f"  Error probing {url}: {e}")

    # --- Step 3: Try HTML-based eFD search pages ---
    # --- Step 3: Fetch efdsearch scripts to find the actual DataTables AJAX URL ---
    log("Probing efdsearch scripts for AJAX endpoint...")
    try:
        r = session.get(f"{EFDSEARCH}/search/home/", timeout=15)
        if r.status_code == 200:
            soup_efd = BeautifulSoup(r.content, "html.parser")
            log(f"  efdsearch/search/home title: {(soup_efd.find('title') or soup_efd).get_text(strip=True)[:80]}")
            for a in soup_efd.find_all("a", href=True)[:30]:
                log(f"  Link: {a.get('href')!r}: {a.get_text(strip=True)[:60]!r}")
            for form in soup_efd.find_all("form"):
                log(f"  Form action={form.get('action')!r} method={form.get('method','get')!r}")
                for inp in form.find_all(["input", "select", "textarea"]):
                    log(f"    {inp.name} name={inp.get('name')!r} value={str(inp.get('value',''))[:80]!r}")
            # Inline scripts (DataTables ajax config will be here)
            for s in soup_efd.find_all("script", src=False):
                t = (s.string or "")
                if len(t) > 20:
                    log(f"  Inline script: {t[:800]!r}")
            # External scripts
            _probe_scripts_for_api(session, soup_efd, EFDSEARCH)
    except Exception as e:
        log(f"  efdsearch probe error: {e}")

    log("WARNING: No PTR filings found. Review logs above for site structure clues.")
    return []


# ---------------------------------------------------------------------------
# Download and parse filing PDFs
# ---------------------------------------------------------------------------

def resolve_pdf_url(session: requests.Session, filing: dict) -> str:
    pdf_url = filing.get("pdf_url", "")
    if pdf_url and pdf_url.endswith(".pdf"):
        return pdf_url

    detail_url = filing.get("pdf_url") or filing.get("detail_url", "")
    if detail_url and not detail_url.endswith(".pdf"):
        if not detail_url.startswith("http"):
            detail_url = BASE + detail_url
        try:
            r = session.get(detail_url, timeout=15)
            if r.status_code == 200:
                s = BeautifulSoup(r.content, "html.parser")
                for a in s.find_all("a", href=True):
                    href = a.get("href", "")
                    if href.endswith(".pdf"):
                        return href if href.startswith("http") else BASE + href
        except Exception as e:
            log(f"  Detail page lookup failed: {e}")

    return pdf_url


def download_and_parse(session: requests.Session, filings: list) -> list:
    all_trades = []
    for filing in filings[:MAX_PDFS]:
        member = filing.get("member", "Unknown")
        filing_date = filing.get("filing_date", "")
        pdf_url = resolve_pdf_url(session, filing)

        if not pdf_url:
            log(f"  No PDF URL for {member}, skipping. Raw: {filing.get('raw', '')}")
            continue

        if not pdf_url.startswith("http"):
            pdf_url = BASE + pdf_url

        try:
            r = session.get(pdf_url, timeout=30)
            if r.status_code != 200:
                log(f"  PDF {pdf_url}: {r.status_code}")
                continue
            trades = parse_ptr_pdf(r.content, member, filing_date)
            log(f"  {member} ({filing_date}): {len(trades)} trades")
            all_trades.extend(trades)
        except Exception as e:
            log(f"  PDF error {pdf_url}: {e}")

    return all_trades


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    now = datetime.utcnow()
    cutoff = now - timedelta(days=LOOKBACK_DAYS)

    log(f"=== Senate PTR sync: {cutoff.date()} → {now.date()} ===")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    existing = []
    if OUTPUT.exists():
        try:
            existing = json.loads(OUTPUT.read_text()).get("trades", [])
            log(f"Loaded {len(existing)} cached trades")
        except Exception:
            pass

    session = requests.Session()
    session.headers.update(HEADERS)

    filings = search_senate_ptrs(session, cutoff, now)
    new_trades = download_and_parse(session, filings) if filings else []

    log(f"Found {len(new_trades)} new trades from {len(filings)} filings")

    def key(t):
        return f"{t['member']}|{t['date']}|{t['ticker']}|{t['type']}"

    merged = {key(t): t for t in existing}
    for t in new_trades:
        merged[key(t)] = t

    cutoff_str = cutoff.strftime("%Y-%m-%d")
    final = sorted(
        [t for t in merged.values() if t.get("date", "") >= cutoff_str],
        key=lambda t: t.get("date", ""),
        reverse=True,
    )

    OUTPUT.write_text(json.dumps({"last_updated": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "trades": final}, indent=2))
    log(f"Wrote {len(final)} trades → {OUTPUT}")
    log("=== Done ===")


if __name__ == "__main__":
    main()
