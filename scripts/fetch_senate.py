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
    Use Playwright to navigate efdsearch, accept the agreement, submit a PTR
    date-range search, and capture the XHR response via network interception.
    Returns a list of filing dicts.
    """
    filings = []
    captured = {}

    def handle_response(response):
        url = response.url
        if "/search/report/data/" in url:
            log(f"  Captured XHR: {url[:100]}  status={response.status}")
            try:
                body = response.body()
                captured["data"] = body
                captured["status"] = response.status
                captured["url"] = url
            except Exception as e:
                log(f"  Failed to read XHR body: {e}")

    page.on("response", handle_response)

    log(f"  Navigating to {EFDSEARCH}/")
    page.goto(EFDSEARCH + "/", wait_until="networkidle", timeout=30000)
    log(f"  Page title: {page.title()}")

    # Accept the prohibition agreement (click the checkbox)
    try:
        checkbox = page.locator("#agree_statement")
        if checkbox.count() > 0:
            log("  Clicking agreement checkbox...")
            checkbox.check()
            page.wait_for_load_state("networkidle", timeout=10000)
            log(f"  After agreement: {page.title()}")
        else:
            log("  No agreement checkbox found — may already be agreed")
    except PWTimeout:
        log("  Agreement checkbox timeout — continuing")
    except Exception as e:
        log(f"  Agreement checkbox error: {e}")

    # Navigate to the search page
    log(f"  Navigating to {EFDSEARCH}/search/home/")
    page.goto(EFDSEARCH + "/search/home/", wait_until="networkidle", timeout=30000)
    log(f"  Search page title: {page.title()}")

    # Log the page content to understand the search form
    page_content = page.content()
    log(f"  Page content size: {len(page_content)} bytes")

    # Look for form fields
    inputs = page.locator("input, select").all()
    log(f"  Found {len(inputs)} form inputs on search page")
    for inp in inputs[:20]:
        try:
            name = inp.get_attribute("name") or ""
            itype = inp.get_attribute("type") or "text"
            val = inp.get_attribute("value") or ""
            log(f"    input name={name!r} type={itype!r} value={val[:40]!r}")
        except Exception:
            pass

    # Try to set the date range and report type
    try:
        # Try common field selectors for date range
        for date_from_sel in ["#fromDate", "[name='fromDate']", "[name='from_date']", "[name='dateFrom']"]:
            loc = page.locator(date_from_sel)
            if loc.count() > 0:
                log(f"  Setting fromDate via {date_from_sel}")
                loc.fill(from_str)
                break

        for date_to_sel in ["#toDate", "[name='toDate']", "[name='to_date']", "[name='dateTo']"]:
            loc = page.locator(date_to_sel)
            if loc.count() > 0:
                log(f"  Setting toDate via {date_to_sel}")
                loc.fill(to_str)
                break

        # Select PTR report type
        for ptr_sel in [
            "select[name='report_types[]']",
            "select[name='report_types']",
            "select[name='reportType']",
            "#report_types",
        ]:
            loc = page.locator(ptr_sel)
            if loc.count() > 0:
                log(f"  Selecting PTR via {ptr_sel}")
                loc.select_option(label="Periodic Transaction Report")
                break

        # Click search button
        for btn_sel in [
            "button[type='submit']",
            "input[type='submit']",
            "#btnSearch",
            ".btn-search",
            "button:has-text('Search')",
            "input[value='Search']",
        ]:
            btn = page.locator(btn_sel)
            if btn.count() > 0:
                log(f"  Clicking search via {btn_sel}")
                btn.first.click()
                page.wait_for_load_state("networkidle", timeout=30000)
                break

    except PWTimeout:
        log("  Search form interaction timed out")
    except Exception as e:
        log(f"  Search form error: {e}")

    # Log what we captured
    log(f"  Captured XHR data: {'yes' if 'data' in captured else 'no'}")
    if "data" in captured:
        try:
            data = json.loads(captured["data"])
            log(f"  XHR JSON preview: {json.dumps(data)[:800]}")

            # Parse DataTables response format: {"data": [...], "recordsTotal": N}
            rows = data.get("data", data.get("results", data.get("filings", [])))
            if isinstance(rows, list):
                log(f"  Rows in response: {len(rows)}")
                for row in rows[:3]:
                    log(f"    Sample row: {json.dumps(row)[:200]}")
                for row in rows:
                    if not isinstance(row, (list, dict)):
                        continue
                    # DataTables rows can be lists or dicts
                    if isinstance(row, list):
                        # Common column order: [name, office/state, filing_type, date, link]
                        member = str(row[0]) if len(row) > 0 else "Unknown"
                        link_html = str(row[-1]) if row else ""
                    else:
                        member = row.get("name") or row.get("filer_name") or "Unknown"
                        link_html = row.get("link") or row.get("pdf_url") or ""

                    # Extract PDF URL from link HTML if it's an HTML string
                    pdf_url = ""
                    if "<a" in link_html:
                        m = re.search(r'href=["\']([^"\']+\.pdf)["\']', link_html, re.I)
                        if m:
                            pdf_url = m.group(1)
                    elif link_html.endswith(".pdf"):
                        pdf_url = link_html

                    filing_date = ""
                    if isinstance(row, dict):
                        filing_date = row.get("date_filed") or row.get("date") or ""

                    if member or pdf_url:
                        filings.append({"member": member, "pdf_url": pdf_url, "filing_date": filing_date})
        except json.JSONDecodeError:
            log(f"  XHR response is not JSON: {captured['data'][:200]}")
        except Exception as e:
            log(f"  XHR parse error: {e}")

    # Also look for results in the page HTML
    results_html = page.content()
    log(f"  Post-search page size: {len(results_html)} bytes")
    if len(results_html) > 20000:  # Larger than the gate page
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(results_html, "html.parser")
        log(f"  Post-search title: {(soup.find('title') or soup).get_text(strip=True)[:80]}")
        tables = soup.find_all("table")
        log(f"  Tables in results: {len(tables)}")
        for t in tables[:2]:
            rows = t.find_all("tr")
            log(f"    Table has {len(rows)} rows")
            for row in rows[:5]:
                log(f"    Row: {row.get_text(separator='|', strip=True)[:120]!r}")
        for a in soup.find_all("a", href=True)[:30]:
            log(f"  Link: {a.get('href')!r}: {a.get_text(strip=True)[:60]!r}")

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

def download_and_parse(filings: list) -> list:
    session = requests.Session()
    session.headers.update(HEADERS)
    all_trades = []

    for filing in filings[:MAX_PDFS]:
        member = filing.get("member", "Unknown")
        filing_date = filing.get("filing_date", "")
        pdf_url = filing.get("pdf_url", "")

        if not pdf_url:
            log(f"  No PDF URL for {member}")
            continue

        if not pdf_url.startswith("http"):
            pdf_url = EFDSEARCH + pdf_url

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
