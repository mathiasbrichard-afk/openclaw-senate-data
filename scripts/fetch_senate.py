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
        # Paginated: {"results": [...]} or {"data": [...]}
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
        # Try common field name variants for member name
        member = (
            item.get("name") or item.get("filer_name") or item.get("first_name", "") + " " + item.get("last_name", "")
            or "Unknown"
        ).strip()
        # Try common field name variants for PDF URL
        pdf_url = (
            item.get("pdf_url") or item.get("document_url") or item.get("link")
            or item.get("url") or item.get("filing_url") or ""
        )
        filing_date = item.get("date_filed") or item.get("date") or item.get("filed_at") or ""
        if member or pdf_url:
            filings.append({"member": member, "pdf_url": pdf_url, "filing_date": filing_date, "raw": item})

    log(f"    Extracted {len(filings)} filings from JSON")
    return filings


def _find_ptr_links_in_html(soup: BeautifulSoup, base: str) -> list:
    """Find PTR PDF links in an HTML page."""
    links = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        text = a.get_text(strip=True)
        if href.lower().endswith(".pdf") or "ptr" in href.lower() or "periodic" in text.lower():
            full = href if href.startswith("http") else base + href
            links.append({"member": text or "Unknown", "pdf_url": full, "filing_date": ""})
    return links


def search_senate_ptrs(session: requests.Session, from_date: datetime, to_date: datetime) -> list:
    """
    Try multiple approaches to find PTR filings on the Senate eFD site.
    Returns list of filing dicts with keys: member, pdf_url, filing_date.
    """
    from_str = from_date.strftime("%Y-%m-%d")
    to_str = to_date.strftime("%Y-%m-%d")

    # --- Step 1: Probe the main page and log everything ---
    log(f"Probing {BASE}/ ...")
    try:
        resp = session.get(BASE + "/", timeout=30)
        log(f"  Status: {resp.status_code}  Size: {len(resp.content)} bytes  CT: {resp.headers.get('Content-Type','?')[:60]}")
        soup = BeautifulSoup(resp.content, "html.parser")
        title = soup.find("title")
        log(f"  Page title: {title.get_text(strip=True) if title else 'N/A'}")

        for form in soup.find_all("form"):
            log(f"  Form action={form.get('action')!r} method={form.get('method','get')!r}")
            for f in form.find_all(["input", "select"]):
                log(f"    {f.name} name={f.get('name')!r} value={str(f.get('value',''))[:50]!r}")

        all_links = [(a.get("href", ""), a.get_text(strip=True)) for a in soup.find_all("a", href=True)]
        log(f"  Links ({len(all_links)} total):")
        for href, text in all_links[:40]:
            log(f"    {href!r}: {text[:60]!r}")

        # Look for inline script snippets mentioning "api"
        for s in soup.find_all("script", src=False):
            t = (s.string or "")
            if "api" in t.lower() and len(t) < 8000:
                log(f"  Inline script (api mention): {t[:400]!r}")

    except Exception as e:
        log(f"  Main page probe failed: {e}")
        return []

    # --- Step 2: Try JSON API patterns ---
    api_urls = [
        # EFTS-style (old URL structure adapted to new domain)
        f"{BASE}/LATEST/search-index?q=&report_types=PTR&dateRange=custom&fromDate={from_str}&toDate={to_str}&results_count=100",
        f"{BASE}/LATEST/search-index?q=&dateRange=custom&fromDate={from_str}&toDate={to_str}&results_count=100",
        # Common REST patterns
        f"{BASE}/api/search?q=&report_types=PTR&dateRange=custom&fromDate={from_str}&toDate={to_str}&results_count=100",
        f"{BASE}/api/filings?filing_type=PTR&from_date={from_str}&to_date={to_str}&limit=100",
        f"{BASE}/api/filings?filing_type=ptr&from_date={from_str}&to_date={to_str}&limit=100",
        f"{BASE}/api/v1/filings?type=PTR&after={from_str}&limit=100",
        f"{BASE}/search?q=&report_types=PTR&dateRange=custom&fromDate={from_str}&toDate={to_str}&results_count=100",
        f"{BASE}/search?filing_type=PTR&from_date={from_str}&to_date={to_str}",
    ]

    for url in api_urls:
        try:
            r = session.get(url, headers={**HEADERS, "Accept": "application/json,text/html,*/*;q=0.8"}, timeout=15)
            ct = r.headers.get("Content-Type", "")
            log(f"  {url[-90:]}: {r.status_code}  CT={ct[:50]}")

            if r.status_code != 200:
                continue

            if "json" in ct:
                data = r.json()
                log(f"    JSON preview: {json.dumps(data)[:600]}")
                filings = _extract_filings_from_json(data)
                if filings:
                    log(f"    SUCCESS: {len(filings)} filings via JSON at {url[-60:]}")
                    return filings
            else:
                # HTML — look for PTR links
                s = BeautifulSoup(r.content, "html.parser")
                log(f"    HTML title: {(s.find('title') or s).get_text(strip=True)[:80]}")
                links = _find_ptr_links_in_html(s, BASE)
                if links:
                    log(f"    SUCCESS: {len(links)} PTR links in HTML at {url[-60:]}")
                    return links
                # Log what we can see
                for a in s.find_all("a", href=True)[:20]:
                    log(f"    Link: {a.get('href')!r}: {a.get_text(strip=True)[:50]!r}")

        except Exception as e:
            log(f"  Error probing {url[-60:]}: {e}")

    # --- Step 3: Try HTML form search ---
    log("Trying HTML form search...")
    try:
        search_pages = [f"{BASE}/search", f"{BASE}/", f"{BASE}/filings"]
        for page_url in search_pages:
            r = session.get(page_url, timeout=15)
            if r.status_code != 200:
                continue
            s = BeautifulSoup(r.content, "html.parser")
            form = s.find("form")
            if not form:
                continue

            action = form.get("action") or page_url
            if not action.startswith("http"):
                action = BASE + action
            method = form.get("method", "get").lower()

            # Build form data
            form_data = {}
            for inp in form.find_all("input"):
                name = inp.get("name")
                if name:
                    form_data[name] = inp.get("value", "")
            for sel in form.find_all("select"):
                name = sel.get("name")
                if name:
                    opt = sel.find("option", selected=True) or sel.find("option")
                    form_data[name] = opt.get("value", "") if opt else ""

            # Add PTR-specific params
            for key in list(form_data.keys()):
                if "type" in key.lower() or "filing" in key.lower():
                    form_data[key] = "PTR"
                if "from" in key.lower() or "start" in key.lower():
                    form_data[key] = from_str
                if "to" in key.lower() or "end" in key.lower():
                    form_data[key] = to_str

            log(f"  Submitting form to {action}: {form_data}")
            fn = session.post if method == "post" else session.get
            r2 = fn(action, data=form_data if method == "post" else None,
                    params=form_data if method == "get" else None, timeout=15)
            log(f"  Form result: {r2.status_code}, {len(r2.content)} bytes")

            s2 = BeautifulSoup(r2.content, "html.parser")
            links = _find_ptr_links_in_html(s2, BASE)
            if links:
                log(f"  Found {len(links)} PTR links via form")
                return links

            log(f"  Form page text: {s2.get_text(separator=' ', strip=True)[:500]}")

    except Exception as e:
        log(f"  Form search error: {e}")

    log("WARNING: No PTR filings found. Site structure unknown — review logs above.")
    return []


# ---------------------------------------------------------------------------
# Download and parse filing PDFs
# ---------------------------------------------------------------------------

def resolve_pdf_url(session: requests.Session, filing: dict) -> str:
    """
    If filing has no pdf_url, try to find it by fetching the filing detail page.
    """
    pdf_url = filing.get("pdf_url", "")
    if pdf_url and pdf_url.endswith(".pdf"):
        return pdf_url

    # Some systems give a filing detail page URL instead of direct PDF
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
                log(f"  PDF {pdf_url[-60:]}: {r.status_code}")
                continue
            trades = parse_ptr_pdf(r.content, member, filing_date)
            log(f"  {member} ({filing_date}): {len(trades)} trades")
            all_trades.extend(trades)
        except Exception as e:
            log(f"  PDF error {pdf_url[-50:]}: {e}")

    return all_trades


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    now = datetime.utcnow()
    cutoff = now - timedelta(days=LOOKBACK_DAYS)

    log(f"=== Senate PTR sync: {cutoff.date()} → {now.date()} ===")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    # Keep existing trades across runs (avoids re-downloading everything)
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

    # Merge new over old by unique key; prune entries older than LOOKBACK_DAYS
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
