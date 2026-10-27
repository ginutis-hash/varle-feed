#!/usr/bin/env python3
"""
citrina.eu prekiu feed'u generatorius (Varle.lt + Google Merchant Center).

Saltiniai:
  * Verskis kaina24.lt XML (KAINA24_URL) - id, pavadinimas, kaina, likutis, EAN, kodas,
    gamintojas, nuotrauka, URL, kategorija.
  * Viesi citrina.eu prekiu puslapiai - pilnas aprasymas (HTML). Talpinama data/pages.json,
    atnaujinama kas PAGE_CACHE_DAYS dienu arba pasikeitus URL.
  * (nebutina) Verskis Google Shopping TSV (GOOGLE_TSV_URL) - sena kaina (price_old) ir
    papildomos nuotraukos, kol modulis dar veikia. Jei nuoroda neveikia - ignoruojama.

Rezultatai:
  docs/varle.xml   - Varle.lt marketplace XML (sablonas varle_xml_mp_(1a_1))
  docs/google.xml  - Google Merchant Center RSS 2.0 feed'as

Aplinkos kintamieji: KAINA24_URL (privalomas), GOOGLE_TSV_URL (nebutinas),
  DELIVERY_TEXT ("1-3 d. d."), EXCLUDE_TITLE_PREFIXES ("[galiojimas", skirti '|'),
  GOOGLE_CATEGORY ("469" = Health & Beauty > Health Care > Fitness & Nutrition > Vitamins & Supplements),
  PAGE_CACHE_DAYS ("7"), OUT_DIR ("docs"), CACHE_FILE ("data/pages.json").
"""
import csv
import html
import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

KAINA24_URL = os.environ.get("KAINA24_URL")
GOOGLE_TSV_URL = os.environ.get("GOOGLE_TSV_URL", "")
DELIVERY_TEXT = os.environ.get("DELIVERY_TEXT", "1-3 d. d.")
GOOGLE_CATEGORY = os.environ.get("GOOGLE_CATEGORY", "469")
PAGE_CACHE_DAYS = int(os.environ.get("PAGE_CACHE_DAYS", "7"))
OUT_DIR = os.environ.get("OUT_DIR", "docs")
CACHE_FILE = os.environ.get("CACHE_FILE", "data/pages.json")
SCRAPE = os.environ.get("SCRAPE", "1") != "0"

UPPER_TOKENS = {"JAV", "USA", "EU", "ES", "DHA", "EPA", "MSM", "CLA", "ALA", "NAC", "TMG", "GMO",
                "B12", "B6", "D3", "K2", "C", "E", "A", "D", "Q10", "CoQ10", "HCL", "HCl", "ZMA", "GABA"}
UNITS = {"MG", "G", "MCG", "ML", "IU", "TV", "KAPS", "TAB", "KAPS.", "TAB.", "VNT", "VNT."}
SMALL_WORDS = {"su", "ir", "be", "iš", "nuo", "iki", "bei", "ar", "dėl", "po", "prie", "už"}
EXCLUDE_TITLE_PREFIXES = [p.strip().lower() for p in
                          os.environ.get("EXCLUDE_TITLE_PREFIXES", "[galiojimas").split("|") if p.strip()]


# ----------------------------------------------------------------- pagalbines

def fetch(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": "citrina-feed-bot/2.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def nice_title(t):
    """BLACK COHOSH 540MG 60 KAPS. (SWANSON, JAV) -> Black Cohosh 540mg 60 kaps. (Swanson, JAV)"""
    t = re.sub(r"\s+", " ", t.strip())
    if t != t.upper():
        return t
    out = []
    for i, w in enumerate(t.split(" ")):
        core = w.strip("(),.;:")
        if core in UPPER_TOKENS:
            out.append(w)
        elif i > 0 and core.lower() in SMALL_WORDS:
            out.append(w.lower())
        elif core.upper() in UNITS:
            out.append(w.lower())
        elif re.search(r"\d", core):
            out.append(re.sub(r"(?<=\d)([A-ZĄČĘĖĮŠŲŪŽ]+)", lambda m: m.group(1).lower(), w))
        elif w.startswith("("):
            out.append("(" + w[1:2] + w[2:].lower())
        else:
            out.append(w[:1] + w[1:].lower())
    return " ".join(out)


def parse_price(s):
    if not s:
        return None
    m = re.search(r"[\d.,]+", s.replace(" ", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except ValueError:
        return None


def cdata(s):
    return "<![CDATA[" + (s or "").replace("]]>", "]]]]><![CDATA[>") + "]]>"


def strip_tags(h):
    h = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", h, flags=re.S | re.I)
    h = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", h, flags=re.I)
    h = re.sub(r"<[^>]+>", " ", h)
    h = html.unescape(h)
    h = re.sub(r"[ \t\r\f\v]+", " ", h)
    h = re.sub(r"\s*\n\s*", "\n", h)
    return h.strip()


# ----------------------------------------------------------------- saltiniai

def load_kaina24(data):
    root = ET.fromstring(data)
    items = {}
    for p in root.findall("product"):
        pid = p.get("id")

        def g(tag, p=p):
            return (p.findtext(tag) or "").strip()

        items[pid] = {"id": pid, "title": g("title"), "price": g("price"), "stock": g("stock"),
                      "ean": g("ean_code"), "code": g("manufacturer_code"),
                      "manufacturer": g("manufacturer"), "image": g("image_url"),
                      "url": g("product_url"), "cat_name": g("category_name"), "cat_id": g("category_id")}
    return items


def load_google_tsv(url):
    """Nebutinas saltinis - jei neveikia, graziname tuscia zodyna."""
    if not url:
        return {}
    try:
        text = fetch(url).decode("utf-8-sig")
    except Exception as e:  # noqa: BLE001
        print(f"Google TSV nepasiekiamas ({e}) - tesiama be jo")
        return {}
    reader = csv.DictReader(text.splitlines(), delimiter="\t", quotechar='"')
    return {r["id"].strip(): r for r in reader if r.get("id")}


def extract_div(page, marker):
    """Grazina HTML tarp <div ... marker ...> ir jo uzdaromojo </div> (balansuojant div'us)."""
    i = page.find(marker)
    if i < 0:
        return ""
    start = page.rfind("<div", 0, i)
    j = page.find(">", i) + 1
    depth, pos = 1, j
    while depth and pos < len(page):
        m = re.compile(r"<div\b|</div\s*>", re.I).search(page, pos)
        if not m:
            break
        depth += 1 if m.group(0).lower().startswith("<div") else -1
        pos = m.end()
    return page[j:pos - 6].strip() if depth == 0 else page[j:pos].strip()


def scrape_pages(items):
    """Nuskaito aprasymus is viesu citrina.eu puslapiu; talpina CACHE_FILE."""
    cache = {}
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, encoding="utf-8") as f:
            cache = json.load(f)
    now = time.time()
    fetched = errors = 0
    for pid, it in items.items():
        if not it["url"]:
            continue
        c = cache.get(pid)
        if c and c.get("url") == it["url"] and now - c.get("ts", 0) < PAGE_CACHE_DAYS * 86400 and c.get("desc_html"):
            continue
        try:
            page = fetch(it["url"], timeout=60).decode("utf-8", "replace")
            desc = extract_div(page, 'id="tab_description"')
            desc = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", desc, flags=re.S | re.I)
            cache[pid] = {"url": it["url"], "ts": now, "desc_html": desc}
            fetched += 1
            time.sleep(0.3)
        except Exception as e:  # noqa: BLE001
            errors += 1
            print(f"  puslapis nenuskaitytas {it['url']}: {e}")
    os.makedirs(os.path.dirname(CACHE_FILE) or ".", exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=0)
    print(f"Puslapiai: nuskaityta {fetched}, klaidu {errors}, talpykloje {len(cache)}")
    return cache


# ----------------------------------------------------------------- normalizavimas

def normalize(kaina, google, pages):
    products, skipped = [], []
    for pid, it in kaina.items():
        g = google.get(pid, {})
        tl = it["title"].strip().lower()
        if any(tl.startswith(p) for p in EXCLUDE_TITLE_PREFIXES):
            skipped.append((pid, it["title"], "ispardavimas (EXCLUDE_TITLE_PREFIXES)"))
            continue
        ean = re.sub(r"\D", "", it["ean"])
        if not ean or len(ean) < 8:
            skipped.append((pid, it["title"], "nera barkodo"))
            continue
        try:
            price = float(it["price"])
        except ValueError:
            price = 0.0
        if price <= 0:
            skipped.append((pid, it["title"], "nera kainos"))
            continue
        if not it["cat_id"]:
            skipped.append((pid, it["title"], "nera kategorijos"))
            continue

        desc_html = (pages.get(pid) or {}).get("desc_html", "").strip()
        if not desc_html:
            txt = (g.get("description") or "").strip() or it["title"]
            desc_html = "<p>" + html.escape(txt) + "</p>"
        desc_text = strip_tags(desc_html)[:4900]

        old = parse_price(g.get("price"))
        price_old = old if old and old > price + 0.009 else None
        try:
            qty = max(int(float(it["stock"] or 0)), 0)
        except ValueError:
            qty = 0
        images = [it["image"]] if it["image"] else []
        extra = (g.get("additional_image_link") or "").strip()
        if extra:
            images += [u.strip() for u in extra.split(",") if u.strip()]

        products.append({"id": pid, "title": nice_title(it["title"]), "desc_html": desc_html,
                         "desc_text": desc_text, "price": price, "price_old": price_old, "qty": qty,
                         "ean": ean, "code": it["code"], "manufacturer": it["manufacturer"],
                         "images": images, "url": it["url"], "cat_id": it["cat_id"],
                         "cat_name": it["cat_name"]})
    return products, skipped


# ----------------------------------------------------------------- Varle.lt

def write_varle(products, now):
    cats = {p["cat_id"]: p["cat_name"] for p in products}
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           f"<!-- citrina.eu -> varle.lt feed, generated {now} -->", "<root>", "  <categories>"]
    for cid, name in sorted(cats.items(), key=lambda x: x[1]):
        out += ["    <category>", f"      <id>{cid}</id>", "      <parent></parent>",
                f"      <name>{cdata(name)}</name>", "    </category>"]
    out += ["  </categories>", "  <products>"]
    for p in products:
        out += ["    <product>", f"      <id>{p['id']}</id>", "      <categories>",
                f"        <category>{p['cat_id']}</category>", "      </categories>",
                f"      <title>{cdata(p['title'])}</title>",
                f"      <description>{cdata(p['desc_html'])}</description>",
                f"      <price>{p['price']:.2f}</price>"]
        if p["price_old"]:
            out.append(f"      <price_old>{p['price_old']:.2f}</price_old>")
        out += [f"      <delivery_text>{DELIVERY_TEXT}</delivery_text>", "      <images>"]
        out += [f"        <image>{cdata(u)}</image>" for u in p["images"]]
        out += ["      </images>", f"      <quantity>{p['qty']}</quantity>",
                f"      <barcode_format>{'UPC' if len(p['ean']) == 12 else 'EAN'}</barcode_format>",
                f"      <barcode>{p['ean']}</barcode>"]
        if p["code"]:
            out.append(f"      <model>{cdata(p['code'])}</model>")
        if p["manufacturer"]:
            out.append(f"      <manufacturer>{cdata(p['manufacturer'])}</manufacturer>")
        if p["url"]:
            out.append(f"      <url>{cdata(p['url'])}</url>")
        out += ["      <product_with_gift>False</product_with_gift>", "    </product>"]
    out += ["  </products>", "</root>", ""]
    return "\n".join(out)


# ----------------------------------------------------------------- Google Merchant Center

def brand_of(manufacturer):
    m = manufacturer.split(",")[0].strip()
    return "Swanson" if m.lower().startswith("swanson") else (m or "Swanson")


def write_google(products, now):
    e = html.escape
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">', "<channel>",
           "<title>citrina.eu</title>", "<link>https://www.citrina.eu/</link>",
           f"<description>citrina.eu prekiu feed'as Google Merchant Center, generated {now}</description>"]
    for p in products:
        price_main = p["price_old"] or p["price"]
        out += ["<item>", f"<g:id>{e(p['id'])}</g:id>", f"<title>{e(p['title'])}</title>",
                f"<description>{e(p['desc_text'])}</description>", f"<link>{e(p['url'])}</link>"]
        if p["images"]:
            out.append(f"<g:image_link>{e(p['images'][0])}</g:image_link>")
            out += [f"<g:additional_image_link>{e(u)}</g:additional_image_link>" for u in p["images"][1:10]]
        out += [f"<g:availability>{'in_stock' if p['qty'] > 0 else 'out_of_stock'}</g:availability>",
                f"<g:price>{price_main:.2f} EUR</g:price>"]
        if p["price_old"]:
            out.append(f"<g:sale_price>{p['price']:.2f} EUR</g:sale_price>")
        out += [f"<g:brand>{e(brand_of(p['manufacturer']))}</g:brand>", f"<g:gtin>{p['ean']}</g:gtin>"]
        if p["code"]:
            out.append(f"<g:mpn>{e(p['code'])}</g:mpn>")
        out += ["<g:condition>new</g:condition>",
                f"<g:product_type>{e('Prekių katalogas > ' + p['cat_name'])}</g:product_type>",
                f"<g:google_product_category>{GOOGLE_CATEGORY}</g:google_product_category>",
                "</item>"]
    out += ["</channel>", "</rss>", ""]
    return "\n".join(out)


# ----------------------------------------------------------------- main

def main():
    if not KAINA24_URL:
        sys.exit("Truksta KAINA24_URL aplinkos kintamojo")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    kaina = load_kaina24(fetch(KAINA24_URL))
    google = load_google_tsv(GOOGLE_TSV_URL)
    pages = scrape_pages(kaina) if SCRAPE else {}
    products, skipped = normalize(kaina, google, pages)

    os.makedirs(OUT_DIR, exist_ok=True)
    for name, text in (("varle.xml", write_varle(products, now)), ("google.xml", write_google(products, now))):
        ET.fromstring(text.encode("utf-8"))  # validumo patikra
        with open(os.path.join(OUT_DIR, name), "w", encoding="utf-8") as f:
            f.write(text)
        print(f"OK: {name} - {len(products)} prekiu")
    scraped = sum(1 for p in products if pages.get(p["id"], {}).get("desc_html"))
    print(f"Aprasymai is svetaines: {scraped}/{len(products)}; su sena kaina: "
          f"{sum(1 for p in products if p['price_old'])}; praleista {len(skipped)}")
    for s in skipped:
        print("  praleista:", s)


if __name__ == "__main__":
    main()
