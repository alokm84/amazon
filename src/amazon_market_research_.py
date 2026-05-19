"""
Amazon.in Market Research Intelligence Agent v2
================================================
Fixes:
  - Robust product link extraction (was returning 0 despite 22 cards found)
  - Multiple fallback selectors for prices, sellers, reviews
  - Better wait strategy before scraping cards
  - Anti-bot: stealth headers, random mouse moves, human scroll

Usage (on your local machine):
    pip install playwright
    playwright install chromium

    # Default — browser opens visibly (great for recording demo videos):
    python amazon_market_research_v2.py --query "boat earbud" --max-products 5

    # Slower, more dramatic for screen recording:
    python amazon_market_research_v2.py --query "boat earbud" --slow-mo 200

    # Headless (background, no window) for production/scheduled runs:
    python amazon_market_research_v2.py --query "boat earbud" --headless
"""

import argparse, csv, os, random, re, sys, time
from datetime import datetime
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


# ── helpers ──────────────────────────────────────────────────────────────────

def pause(lo=0.8, hi=2.2):
    time.sleep(random.uniform(lo, hi))

def human_scroll(page, steps=6):
    for _ in range(steps):
        page.mouse.wheel(0, random.randint(200, 500))
        time.sleep(random.uniform(0.25, 0.7))

def clean(t):
    return " ".join((t or "").split()).strip()

def first_text(page, *selectors):
    """Try selectors in order, return first non-empty text found."""
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if el.count():
                t = clean(el.inner_text())
                if t:
                    return t
        except Exception:
            pass
    return ""

def extract_price(raw):
    return re.sub(r"[^\d,.]", "", raw or "").strip()


# ── browser factory ──────────────────────────────────────────────────────────

def make_browser(pw, headless=True, slow_mo=120):
    browser = pw.chromium.launch(
        headless=True,
        slow_mo=slow_mo,          # slows every action so it looks natural on screen
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--start-maximized",  # opens full screen — looks great on recording
        ],
    )
    ctx = browser.new_context(
        viewport={"width": 1440, "height": 900},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="en-IN",
        timezone_id="Asia/Kolkata",
        extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"},
    )
    ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
    )
    return browser, ctx


# ── search + link extraction (THE BUG WAS HERE) ─────────────────────────────

def search_and_get_links(page, query, max_products):
    print(f"\n[→] Opening Amazon.in …")
    page.goto("https://www.amazon.in", timeout=40_000)
    page.wait_for_load_state("domcontentloaded")
    pause(1.5, 2.5)

    # Type in search box
    box = page.locator("#twotabsearchtextbox")
    box.wait_for(timeout=15_000)
    box.click()
    pause(0.3, 0.7)
    for ch in query:
        box.type(ch, delay=random.randint(70, 150))
    pause(0.4, 0.8)
    page.keyboard.press("Enter")

    # Wait for results — use a reliable signal
    page.wait_for_selector(
        '[data-component-type="s-search-result"], .s-result-item[data-asin]',
        timeout=20_000,
    )
    pause(1.5, 2.5)
    human_scroll(page, steps=5)
    pause(1.0, 1.5)

    # ── THE FIX: use evaluate() to pull data directly from the DOM ──────────
    # This avoids the Playwright locator chaining bug where .all() on a
    # child locator of a card returns 0 elements even though they exist.
    links = page.evaluate(
        """(maxN) => {
            const cards = document.querySelectorAll(
                '[data-component-type="s-search-result"][data-asin]'
            );
            const results = [];
            for (const card of cards) {
                const asin = card.getAttribute('data-asin');
                if (!asin) continue;
                // grab the first product link inside this card
                const a = card.querySelector('h2 a, a.a-link-normal[href*="/dp/"]');
                if (!a) continue;
                const href = a.href;          // absolute URL from the DOM
                const titleEl = card.querySelector('h2 span, h2 a span');
                const title = titleEl ? titleEl.innerText.trim() : '';
                results.push({ asin, url: href, title });
                if (results.length >= maxN) break;
            }
            return results;
        }""",
        max_products,
    )

    print(f"[i] Found {len(links)} product links")
    return links


# ── product detail ────────────────────────────────────────────────────────────

def scrape_product(page, p):
    print(f"\n[→] {p['title'][:65]} …")
    page.goto(p["url"], timeout=40_000)
    page.wait_for_load_state("domcontentloaded")
    pause(1.5, 2.5)
    human_scroll(page, steps=4)

    row = {
        "ASIN": p["asin"],
        "Product Title": p["title"],
        "Product URL": p["url"],
        "Seller Name": "",
        "Original Price MRP": "",
        "Discounted Price": "",
        "Discount %": "",
        "Rating": "",
        "Total Reviews": "",
        "Critical Review Title": "",
        "Critical Review Rating": "",
        "Critical Review Body": "",
        "Scraped At": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    # Prices via JS evaluation (most reliable)
    prices = page.evaluate("""() => {
        const get = sel => {
            const el = document.querySelector(sel);
            return el ? el.innerText.trim() : '';
        };
        return {
            sale:     get('.apexPriceToPay .a-offscreen')
                   || get('#priceblock_ourprice')
                   || get('#priceblock_dealprice')
                   || get('.a-price.priceToPay .a-offscreen'),
            mrp:      get('.basisPrice .a-offscreen')
                   || get('span.a-price.a-text-price .a-offscreen')
                   || get('#priceblock_listprice'),
            discount: get('.savingsPercentage')
                   || get('.a-color-price.a-size-base.a-text-bold'),
        };
    }""")
    row["Discounted Price"]    = extract_price(prices.get("sale", ""))
    row["Original Price MRP"]  = extract_price(prices.get("mrp", ""))
    row["Discount %"]          = clean(prices.get("discount", "")).lstrip("-")

    # Seller
    row["Seller Name"] = first_text(
        page,
        "#sellerProfileTriggerId",
        "#merchant-info a",
        ".tabular-buybox-text[tabindex] a",
        "#tabular-buybox-truncate-0 a",
        ".offer-display-feature-text a",
    )
    if not row["Seller Name"]:
        row["Seller Name"] = first_text(page, "#merchant-info")

    # Rating
    row["Rating"] = first_text(
        page,
        "#acrPopover .a-icon-alt",
        "#averageCustomerReviews .a-icon-alt",
    ).split()[0] if first_text(page, "#acrPopover .a-icon-alt") else ""

    # Total reviews
    row["Total Reviews"] = first_text(page, "#acrCustomerReviewText")

    # Critical review
    get_critical_review(page, p["asin"], row)
    return row


def get_critical_review(page, asin, row):
    url = (
        f"https://www.amazon.in/product-reviews/{asin}"
        f"?filterByStar=one_star&sortBy=recent&pageNumber=1"
    )
    try:
        print(f"   [→] Fetching 1-star reviews …")
        page.goto(url, timeout=30_000)
        page.wait_for_load_state("domcontentloaded")
        pause(1.2, 2.0)
        human_scroll(page, steps=3)

        data = page.evaluate("""() => {
            const rev = document.querySelector('[data-hook="review"]');
            if (!rev) return null;
            const ratingEl  = rev.querySelector('[data-hook="review-star-rating"] .a-icon-alt');
            const titleEl   = rev.querySelector('[data-hook="review-title"] span:last-child');
            const bodyEl    = rev.querySelector('[data-hook="review-body"] span');
            return {
                rating: ratingEl ? ratingEl.innerText.trim() : '',
                title:  titleEl  ? titleEl.innerText.trim()  : '',
                body:   bodyEl   ? bodyEl.innerText.trim()   : '',
            };
        }""")

        if data:
            row["Critical Review Rating"] = data.get("rating", "")
            row["Critical Review Title"]  = data.get("title", "")
            row["Critical Review Body"]   = data.get("body", "")[:600]

    except PWTimeout:
        print("   [!] Reviews page timed out")
    except Exception as e:
        print(f"   [!] Reviews error: {e}")


# ── CSV ───────────────────────────────────────────────────────────────────────

FIELDS = [
    "ASIN", "Product Title", "Product URL", "Seller Name",
    "Original Price MRP", "Discounted Price", "Discount %",
    "Rating", "Total Reviews",
    "Critical Review Title", "Critical Review Rating", "Critical Review Body",
    "Scraped At",
]

def save_csv(results, query, out_dir="data"):
    """
    Saves each run into:
        data/
            boat_earbud_20260517_143000.csv
            noise_headphone_20260517_150000.csv
    So every query gets its own clearly named file — nothing mixed.
    """
    data_dir = Path(out_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    ts          = datetime.now().strftime("%Y%m%d_%H%M%S")
    clean_query = re.sub(r"[^\w]", "_", query.strip().lower())  # "boat earbud" -> "boat_earbud"
    filename    = f"{clean_query}_{ts}.csv"
    path        = data_dir / filename

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for row in results:
            w.writerow({k: row.get(k, "") for k in FIELDS})

    print(f"\n📁 Folder : {data_dir.resolve()}")
    print(f"📄 File   : {filename}")
    return str(path)


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query",        default="boat earbud")
    ap.add_argument("--max-products", type=int, default=5)
    ap.add_argument("--headless",     action="store_true", default=False)   # visible by default
    ap.add_argument("--slow-mo",      type=int, default=120)                # ms delay per action
    ap.add_argument("--output-dir",   default="data")  # saves to ./data/ folder by default
    args = ap.parse_args()

    print("=" * 60)
    print("  Amazon.in Market Research Agent v2")
    print(f"  Query: {args.query} | Products: {args.max_products}")
    print("=" * 60)

    results = []
    with sync_playwright() as pw:
        browser, ctx = make_browser(pw, headless=args.headless, slow_mo=args.slow_mo)
        page = ctx.new_page()
        try:
            links = search_and_get_links(page, args.query, args.max_products)
            for i, link in enumerate(links, 1):
                print(f"\n── Product {i}/{len(links)} ──")
                try:
                    row = scrape_product(page, link)
                    results.append(row)
                    print(f"   Seller : {row['Seller Name'] or 'N/A'}")
                    print(f"   MRP    : ₹{row['Original Price MRP'] or 'N/A'}"
                          f"  →  ₹{row['Discounted Price'] or 'N/A'}"
                          f"  ({row['Discount %'] or 'N/A'} off)")
                    print(f"   Issue  : {row['Critical Review Title'] or 'N/A'}")
                    pause(2.5, 4.5)
                except Exception as e:
                    print(f"   [!] Failed: {e}")
        finally:
            ctx.close()
            browser.close()

    if results:
        path = save_csv(results, args.query, args.output_dir)
        print(f"\n✅  {len(results)} products saved → {path}")
    else:
        print("\n⚠️  No results collected.")
        sys.exit(1)

if __name__ == "__main__":
    main()
