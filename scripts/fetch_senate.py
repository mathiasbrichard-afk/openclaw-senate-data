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


def _parse_ptr_transactions_from_html(html: str, member: str, filing_date: str) -> list:
    """
    Parse electronic PTR transaction data from eFD rendered HTML.

    eFD table columns (confirmed from live data):
      [0:'#', 1:'Transaction Date', 2:'Owner', 3:'Ticker', 4:'Asset Name',
       5:'Asset Type', 6:'Type', 7:'Amount', 8:'Comment']
    """
    from bs4 import BeautifulSoup
    trades = []
    soup = BeautifulSoup(html, "html.parser")

    title = soup.find("title")
    log(f"    PTR HTML title: {title.get_text(strip=True) if title else 'N/A'}")
    tables = soup.find_all("table")
    log(f"    Tables found: {len(tables)}")
    for i, t in enumerate(tables[:3]):
        rows = t.find_all("tr")
        log(f"    Table {i}: {len(rows)} rows")
        for row in rows[:3]:
            log(f"      {row.get_text(separator='|', strip=True)[:120]!r}")

    for table in tables:
        header_row = table.find("tr")
        if not header_row:
            continue
        headers = [th.get_text(strip=True).lower() for th in header_row.find_all(["th", "td"])]
        log(f"    Headers: {headers}")

        # Must match the eFD electronic PTR table structure
        if not ("transaction date" in headers and "ticker" in headers and "amount" in headers):
            continue

        for row in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cells) < 7:
                continue

            # Column mapping: [#, date, owner, ticker, asset_name, asset_type, type, amount, comment]
            date_str = cells[1]
            owner    = cells[2]
            ticker   = cells[3]   # plain ticker: 'SBUX', 'NVDA', etc.
            asset    = cells[4]
            tx_type  = cells[6]
            amount_raw = cells[7] if len(cells) > 7 else ""

            # Accept only valid exchange tickers (1-5 uppercase letters)
            if not ticker or not re.match(r'^[A-Z]{1,5}$', ticker):
                continue

            tx_date = None
            for fmt in ["%m/%d/%Y", "%Y-%m-%d"]:
                try:
                    tx_date = datetime.strptime(date_str, fmt)
                    break
                except ValueError:
                    continue
            if not tx_date:
                continue

            trades.append({
                "member": member,
                "chamber": "Senate",
                "owner": owner,
                "asset": asset,
                "ticker": ticker,
                "type": _normalize_type(tx_type),
                "date": tx_date.strftime("%Y-%m-%d"),
                "filing_date": filing_date,
                "amount_raw": amount_raw,
                "amount_lower": _amount_lower(amount_raw),
            })

    return trades


def _resolve_pdfs_via_playwright(page, filing_stubs: list) -> list:
    """
    For every filing:
      1. Navigate to the detail page — this establishes session/Referer context and
         lets us find the print URL from the rendered HTML.
      2a. paper type: navigate to the print URL found on the detail page; the print page
          loads the PDF scan as a sub-resource via JavaScript. Intercept that PDF response.
      2b. ptr type: parse the JS-rendered transaction table from the detail page HTML, or
          intercept any JSON transaction API response that the detail page triggers.
    """
    all_trades = []

    for filing in filing_stubs[:MAX_PDFS]:
        member = filing.get("member", "Unknown")
        filing_date = filing.get("filing_date", "")
        detail_url = filing.get("detail_url", "")
        if not detail_url:
            continue

        parts = detail_url.rstrip("/").split("/")
        uuid = parts[-1] if parts else ""
        view_type = parts[-2] if len(parts) >= 2 else ""
        log(f"  {member} ({filing_date}): type={view_type}")

        captured = {}

        def make_handler(cap):
            def handle_response(response):
                ct = response.headers.get("content-type", "")
                url = response.url
                if "pdf" in ct.lower():
                    log(f"    PDF intercepted: {url[:80]} ({len(response.body())} bytes)")
                    if "pdf" not in cap:  # first PDF wins
                        cap["pdf"] = response.body()
                elif "json" in ct.lower() and any(k in url for k in ("transaction", "/ptr", "/report")):
                    log(f"    JSON intercepted: {url[:80]}")
                    cap["json"] = response.body()
                    cap["json_url"] = url
            return handle_response

        handler = make_handler(captured)
        page.on("response", handler)

        # Step 1: visit detail page (establishes session context)
        try:
            page.goto(detail_url, wait_until="networkidle", timeout=25000)
            log(f"    Detail page: {page.title()!r}  url={page.url}")
        except Exception as e:
            log(f"    Detail nav error: {e}")

        # Step 2a: for paper filings, follow the print link
        if "pdf" not in captured and view_type == "paper":
            # Try to get the print URL from rendered page links first
            print_url = None
            try:
                for link in page.locator("a").all():
                    href = link.get_attribute("href") or ""
                    if "/print/" in href:
                        print_url = href if href.startswith("http") else EFDSEARCH + href
                        log(f"    Found print link: {print_url}")
                        break
            except Exception as e:
                log(f"    Link probe error: {e}")
            if not print_url:
                print_url = f"{EFDSEARCH}/search/print/paper/{uuid}/"
                log(f"    Using constructed print URL: {print_url}")
            try:
                page.goto(print_url, wait_until="networkidle", timeout=25000)
                log(f"    Print page title: {page.title()!r}  body_len={len(page.content())}")
                # Log first 800 chars of print page source for debugging
                content = page.content()
                log(f"    Print source preview: {content[:800]!r}")
            except Exception as e:
                log(f"    Print page nav error: {e}")

        page.remove_listener("response", handler)

        # Process what was captured
        if "pdf" in captured:
            trades = parse_ptr_pdf(captured["pdf"], member, filing_date)
            log(f"  {member} ({filing_date}): {len(trades)} trades (PDF)")
            all_trades.extend(trades)
            continue

        if "json" in captured:
            try:
                data = json.loads(captured["json"])
                log(f"  JSON ({captured.get('json_url','?')[-60:]}): {json.dumps(data)[:500]}")
                rows = data.get("transactions", data.get("data", data.get("trades", [])))
                count_before = len(all_trades)
                for row in rows:
                    ticker = row.get("ticker") or _extract_ticker(str(row.get("asset", "")))
                    if ticker == "N/A":
                        continue
                    try:
                        d = datetime.strptime(row.get("transaction_date", ""), "%m/%d/%Y")
                        date_str = d.strftime("%Y-%m-%d")
                    except Exception:
                        date_str = row.get("transaction_date", "")
                    all_trades.append({
                        "member": member, "chamber": "Senate",
                        "owner": row.get("owner", "Member"),
                        "asset": row.get("asset_name", row.get("asset", "")),
                        "ticker": ticker,
                        "type": _normalize_type(row.get("transaction_type", "")),
                        "date": date_str,
                        "filing_date": filing_date,
                        "amount_raw": row.get("amount", ""),
                        "amount_lower": _amount_lower(row.get("amount", "")),
                    })
                log(f"  {member} ({filing_date}): {len(all_trades)-count_before} trades from JSON")
                continue
            except Exception as e:
                log(f"  JSON parse error: {e}")

        # Fallback: parse rendered HTML table from the detail page
        try:
            # Navigate back to detail page since we may have moved to print page
            if view_type == "paper":
                page.goto(detail_url, wait_until="networkidle", timeout=20000)
            html_content = page.content()
            log(f"    Rendered detail HTML: {len(html_content)} bytes")
            # Log sample of HTML for structure discovery
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html_content, "html.parser")
            log(f"    Page title: {soup.find('title') and soup.find('title').get_text(strip=True)!r}")
            tables = soup.find_all("table")
            log(f"    Tables: {len(tables)}")
            for i, t in enumerate(tables[:4]):
                rows_t = t.find_all("tr")
                log(f"      Table {i} ({len(rows_t)} rows): {rows_t[0].get_text(separator='|', strip=True)[:100]!r}" if rows_t else f"      Table {i}: empty")
            trades = _parse_ptr_transactions_from_html(html_content, member, filing_date)
            if trades:
                log(f"  {member} ({filing_date}): {len(trades)} trades (HTML table)")
                all_trades.extend(trades)
            else:
                log(f"  No trades parsed for {member} ({filing_date})")
        except Exception as e:
            log(f"  HTML fallback error: {e}")

    return all_trades


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
            filing_stubs = _intercept_search_response(page, from_str, to_str)
            log(f"Resolving PDFs for {len(filing_stubs)} filings in browser session...")
            all_trades = _resolve_pdfs_via_playwright(page, filing_stubs)
        finally:
            browser.close()

    log(f"Playwright session complete: {len(all_trades)} trades extracted")
    return all_trades


# ---------------------------------------------------------------------------
# download_and_parse is now handled inside Playwright session
# ---------------------------------------------------------------------------

def download_and_parse(trades: list) -> list:
    # Trades are now returned directly from search_senate_ptrs
    return trades


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
