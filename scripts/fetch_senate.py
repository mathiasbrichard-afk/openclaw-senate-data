#!/usr/bin/env python3
"""
Senate STOCK Act PTR scraper — uses Playwright (headless Chromium) to bypass
Akamai WAF on efdsearch.senate.gov, which requires real browser execution.

Runs on GitHub Actions (installs Chromium via playwright install).
Outputs data/senate_ptrs.json — rolling 90-day window of Senate stock trades.
"""

import io
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pdfplumber
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

EFDSEARCH = "https://efdsearch.senate.gov"
OUTPUT = Path("data/senate_ptrs.json")
LOOKBACK_DAYS = 90
MAX_PDFS = 60

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


def log(msg):
    now = datetime.utcnow()
    print(f"[{now:%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# PDF parsing
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
# Playwright browser scraper
# ---------------------------------------------------------------------------

def _intercept_search_response(page, from_str: str, to_str: str) -> list:
    """
    Use Playwright to navigate efdsearch, accept agreement, filter by PTR + date range,
    capture the XHR response, and return filing dicts with detail page URLs.

    Row format from DataTables: [first_name, last_name, affiliation, link_html, date_filed]
    The link_html contains a detail view URL like /search/view/ptr/{UUID}/
    """
    filings = []
    # We'll collect ALL XHR hits; the last one after submit is the filtered result
    xhrs = []

    def handle_response(response):
        url = response.url
        if "/search/report/data/" in url:
            log(f"  XHR hit: {url[:100]}  status={response.status}")
            try:
                body = response.body()
                xhrs.append({"data": body, "status": response.status, "url": url})
            except Exception as e:
                log(f"  Failed to read XHR body: {e}")

    page.on("response", handle_response)

    log(f"  Navigating to {EFDSEARCH}/")
    page.goto(EFDSEARCH + "/", wait_until="networkidle", timeout=30000)
    log(f"  Page title: {page.title()}")

    # Accept the prohibition agreement
    try:
        checkbox = page.locator("#agree_statement")
        if checkbox.count() > 0:
            log("  Clicking agreement checkbox...")
            with page.expect_navigation(wait_until="networkidle", timeout=15000):
                checkbox.check()
            log(f"  After agreement URL: {page.url}  title: {page.title()}")
        else:
            log("  No agreement checkbox found — may already be agreed")
    except PWTimeout:
        log(f"  Agreement navigation timeout — current URL: {page.url}")
    except Exception as e:
        log(f"  Agreement checkbox error: {e}")

    # Navigate to search page only if not already there
    current_url = page.url
    log(f"  Current URL after agreement: {current_url}")
    if "/search/" not in current_url:
        try:
            page.goto(EFDSEARCH + "/search/home/", wait_until="networkidle", timeout=30000)
        except Exception as e:
            log(f"  goto /search/home/ error: {e}")
    else:
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except PWTimeout:
            pass
    log(f"  Search page: {page.title()}  URL: {page.url}")

    # Log form inputs
    inputs = page.locator("input, select").all()
    log(f"  Found {len(inputs)} form inputs")
    for inp in inputs[:25]:
        try:
            name = inp.get_attribute("name") or ""
            itype = inp.get_attribute("type") or "text"
            val = inp.get_attribute("value") or ""
            eid = inp.get_attribute("id") or ""
            log(f"    input id={eid!r} name={name!r} type={itype!r} value={val[:40]!r}")
        except Exception:
            pass

    # Clear previous XHR captures (page load default search)
    xhrs.clear()

    # Set filters
    try:
        # PTR report type: checkbox name='report_type' value='11'
        ptr_cb = page.locator("input[name='report_type'][value='11']")
        if ptr_cb.count() > 0:
            log("  Checking PTR checkbox (report_type=11)...")
            if not ptr_cb.is_checked():
                ptr_cb.check()

        # Uncheck any other report_type checkboxes that might be checked by default
        for cb in page.locator("input[name='report_type']").all():
            val = cb.get_attribute("value") or ""
            if val != "11" and cb.is_checked():
                log(f"  Unchecking report_type={val}")
                cb.uncheck()

        # Date range fields
        for sel in ["#fromDate", "[name='fromDate']", "[name='from_date']"]:
            loc = page.locator(sel)
            if loc.count() > 0:
                loc.fill(from_str)
                log(f"  fromDate set to {from_str} via {sel}")
                break

        for sel in ["#toDate", "[name='toDate']", "[name='to_date']"]:
            loc = page.locator(sel)
            if loc.count() > 0:
                loc.fill(to_str)
                log(f"  toDate set to {to_str} via {sel}")
                break

        # Submit search
        for btn_sel in [
            "#btnSearch",
            "button[type='submit']",
            "input[type='submit']",
            "button:has-text('Search')",
            "input[value='Search']",
        ]:
            btn = page.locator(btn_sel)
            if btn.count() > 0:
                log(f"  Clicking search via {btn_sel}")
                btn.first.click()
                page.wait_for_load_state("networkidle", timeout=30000)
                log(f"  After search: {page.title()}")
                break

    except PWTimeout:
        log("  Search form interaction timed out")
    except Exception as e:
        log(f"  Search form error: {e}")

    # Use the LAST captured XHR (filtered results after form submit)
    log(f"  Total XHRs captured: {len(xhrs)}")
    if not xhrs:
        log("  No XHR captured — no filings found")
        return filings

    captured_data = xhrs[-1]["data"]
    try:
        data = json.loads(captured_data)
        total = data.get("recordsTotal", "?")
        log(f"  XHR recordsTotal={total}  preview: {json.dumps(data)[:400]}")

        rows = data.get("data", [])
        log(f"  Parsing {len(rows)} rows...")
        for i, row in enumerate(rows[:3]):
            log(f"  Sample row {i}: {json.dumps(row)[:200]}")

        for row in rows:
            if not isinstance(row, list) or len(row) < 4:
                continue
            # Row: [first_name, last_name, affiliation, link_html, date_filed]
            first = str(row[0]).strip()
            last = str(row[1]).strip()
            member = f"{first} {last}".strip()
            link_html = str(row[3]) if len(row) > 3 else ""
            filing_date = str(row[4]).strip() if len(row) > 4 else ""

            # Extract detail view URL (not a PDF — we'll follow it later)
            detail_url = ""
            m = re.search(r'href=["\']([^"\']+)["\']', link_html)
            if m:
                detail_url = m.group(1)
                if not detail_url.startswith("http"):
                    detail_url = EFDSEARCH + detail_url

            filings.append({
                "member": member,
                "pdf_url": "",           # filled in download_and_parse
                "detail_url": detail_url,
                "filing_date": filing_date,
            })

    except json.JSONDecodeError:
        log(f"  XHR not JSON: {captured_data[:200]}")
    except Exception as e:
        log(f"  XHR parse error: {e}")

    log(f"  Returning {len(filings)} filing stubs (detail URLs to resolve)")
    return filings


def search_senate_ptrs(from_date: datetime, to_date: datetime) -> list:
    from_str = from_date.strftime("%m/%d/%Y")
    to_str = to_date.strftime("%m/%d/%Y")
    log(f"Launching Playwright (Chromium) for date range {from_str} → {to_str}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="en-US",
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()
        try:
            filings = _intercept_search_response(page, from_str, to_str)
        finally:
            browser.close()

    log(f"Found {len(filings)} filings via Playwright")
    return filings


# ---------------------------------------------------------------------------
# Download and parse filing PDFs
# ---------------------------------------------------------------------------

def _resolve_pdf_from_detail(session: requests.Session, detail_url: str) -> str:
    """
    Follow a Senate eFD detail view URL to find the actual PDF download link.
    Detail pages are at /search/view/{type}/{uuid}/ and contain a PDF link.
    """
    try:
        r = session.get(detail_url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            log(f"  Detail page {detail_url}: {r.status_code}")
            return ""
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.content, "html.parser")
        # Look for PDF links
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            text = a.get_text(strip=True).lower()
            if href.lower().endswith(".pdf") or "pdf" in text or "download" in text or "view" in text.lower():
                full = href if href.startswith("http") else EFDSEARCH + href
                if ".pdf" in full.lower() or "/view/" in full.lower():
                    log(f"    PDF link: {full[:80]}")
                    return full
        # Log page content for debugging
        log(f"  Detail page {detail_url}: no PDF link found. Links: {[a.get('href','')[:50] for a in soup.find_all('a', href=True)[:10]]}")
    except Exception as e:
        log(f"  Detail page error {detail_url}: {e}")
    return ""


def download_and_parse(filings: list) -> list:
    session = requests.Session()
    session.headers.update(HEADERS)
    all_trades = []

    for filing in filings[:MAX_PDFS]:
        member = filing.get("member", "Unknown")
        filing_date = filing.get("filing_date", "")
        pdf_url = filing.get("pdf_url", "")
        detail_url = filing.get("detail_url", "")

        # Resolve PDF URL from detail page if not already known
        if not pdf_url and detail_url:
            pdf_url = _resolve_pdf_from_detail(session, detail_url)

        if not pdf_url:
            # Try constructing PDF URL from detail URL pattern
            # /search/view/ptr/{UUID}/ → try /search/view/ptr/{UUID}/print_annual_ptr/
            if detail_url and "/search/view/" in detail_url:
                for suffix in ["/print_annual_ptr/", "/print/", ".pdf"]:
                    candidate = detail_url.rstrip("/") + suffix
                    try:
                        r = session.head(candidate, headers=HEADERS, timeout=10, allow_redirects=True)
                        if r.status_code == 200:
                            pdf_url = candidate
                            log(f"  Constructed PDF URL: {pdf_url}")
                            break
                    except Exception:
                        pass

        if not pdf_url:
            log(f"  No PDF for {member} (detail: {detail_url[:60]})")
            continue

        if not pdf_url.startswith("http"):
            pdf_url = EFDSEARCH + pdf_url

        try:
            r = session.get(pdf_url, headers=HEADERS, timeout=30)
            if r.status_code != 200:
                log(f"  PDF {pdf_url}: {r.status_code}")
                continue
            ct = r.headers.get("Content-Type", "")
            if "pdf" not in ct.lower() and not pdf_url.lower().endswith(".pdf"):
                log(f"  Not a PDF ({ct}): {pdf_url[:80]}")
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

    filings = search_senate_ptrs(cutoff, now)
    new_trades = download_and_parse(filings) if filings else []

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
