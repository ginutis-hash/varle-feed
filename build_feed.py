#!/usr/bin/env python3
"""
citrina.eu prekiu feed'u generatorius: Varle.lt + Google Merchant Center + kaina24.lt.

Duomenu saltiniai (pagal prioriteta):
  1. Viesi citrina.eu puslapiai - VISADA: /products.xml (prekiu sarasas) ir kiekvienos prekes
     puslapis: id (good_id), pavadinimas, kaina, sena kaina, likutis (yra/nera), nuotrauka,
     aprasymas (HTML), kategorija (BreadcrumbList), gamintojas.
  2. Verskis kaina24.lt XML (KAINA24_URL) - KOL VEIKIA: tikslus likucio skaicius, EAN, prekes
     kodas, kategorijos ID. Is jo kiekviena karta atnaujinamas data/catalog.json (id -> EAN,
     kodas, kategorija), kuris lieka naudojamas ir modulį isjungus.
  3. Verskis Google TSV (GOOGLE_TSV_URL) - nebutinas, nebenaudojamas (paliktas suderinamumui).

Rezultatai (OUT_DIR, numatyta docs/):
  varle.xml    - Varle.lt marketplace XML
  google.xml   - Google Merchant Center RSS 2.0
  kaina24.xml  - kaina24.lt / hind.ee formato XML (tas pats, kaip Verskis eksportas)

Aplinkos kintamieji: KAINA24_URL (nebutinas), DELIVERY_TEXT ("1-3 d. d."),
  EXCLUDE_TITLE_PREFIXES ("[galiojimas", skirti '|'), GOOGLE_CATEGORY ("469"),
  DEFAULT_QTY ("10" - kiekis, kai likutis zinomas tik kaip "yra"), SITE ("https://www.citrina.eu"),
  OUT_DIR, CATALOG_FILE ("data/catalog.json"), PAGES_FILE ("data/pages.json").
"""
import html
import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

SITE = os.environ.get("SITE", "https://www.citrina.eu").rstrip("/")
KAINA24_URL = os.environ.get("KAINA24_URL", "")
DELIVERY_TEXT = os.environ.get("DELIVERY_TEXT", "1-3 d. d.")
GOOGLE_CATEGORY = os.environ.get("GOOGLE_CATEGORY", "469")
DEFAULT_QTY = int(os.environ.get("DEFAULT_QTY", "10"))
OUT_DIR = os.environ.get("OUT_DIR", "docs")
CATALOG_FILE = os.environ.get("CATALOG_FILE", "data/catalog.json")
PAGES_FILE = os.environ.get("PAGES_FILE", "data/pages.json")
SCRAPE = os.environ.get("SCRAPE", "1") != "0"

UPPER_TOKENS = {"JAV", "USA", "EU", "ES", "DHA", "EPA", "MSM", "CLA", "ALA", "NAC", "TMG", "GMO",
                "B12", "B6", "D3", "K2", "C", "E", "A", "D", "Q10", "CoQ10", "HCL", "HCl", "ZMA", "GABA"}
UNITS = {"MG", "G", "MCG", "ML", "IU", "TV", "KAPS", "TAB", "KAPS.", "TAB.", "VNT", "VNT."}
SMALL_WORDS = {"su", "ir", "be", "iš", "nuo", "iki", "bei", "ar", "dėl", "po", "prie", "už"}
EXCLUDE_TITLE_PREFIXES = [p.strip().lower() for p in
                          os.environ.get("EXCLUDE_TITLE_PREFIXES", "[galiojimas").split("|") if p.strip()]


# ----------------------------------------------------------------- pagalbines

def fetch(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": "citrina-feed-bot/3.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=0)


def nice_title(t):
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


def extract_div(page, marker):
    i = page.find(marker)
    if i < 0:
        return ""
    j = page.find(">", i) + 1
    depth, pos = 1, j
    pat = re.compile(r"<div\b|</div\s*>", re.I)
    while depth and pos < len(page):
        m = pat.search(page, pos)
        if not m:
            break
        depth += 1 if m.group(0).lower().startswith("<div") else -1
        pos = m.end()
    return page[j:pos - 6].strip() if depth == 0 else page[j:pos].strip()


def price_from_markup(fragment):
    """'8<sup class=..>97</sup>&nbsp;<span>€</span>' -> 8.97"""
    txt = re.sub(r"<[^>]+>", "|", html.unescape(fragment))
    m = re.search(r"(\d+)\|+(\d{1,2})\|", txt + "|")
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    m = re.search(r"(\d+[.,]\d{1,2})", txt)
    return float(m.group(1).replace(",", ".")) if m else None


# ----------------------------------------------------------------- svetaines nuskaitymas

def product_urls():
    xml = fetch(f"{SITE}/products.xml").decode("utf-8", "replace")
    return [m.group(1).strip() for m in re.finditer(r"<loc>([^<]+)</loc>", xml)]


def parse_page(page, url):
    g = lambda re_, flags=re.S | re.I: (lambda m: m.group(1) if m else "")(re.search(re_, page, flags))
    pid = g(r'name="good_id"\s+value="(\d+)"')
    title = html.unescape(strip_tags(g(r"<h1[^>]*>(.*?)</h1>")))
    price = price_from_markup(g(r'<strong[^>]*id="price"[^>]*>(.*?)</strong>'))
    old = price_from_markup(g(r'<s[^>]*class="[^"]*old-price[^"]*"[^>]*>(.*?)</s>'))
    status = extract_div(page, 'id="good_status"')
    in_stock = bool(re.search(r'class="[^"]*\bin-stock\b', status)) and not re.search(r"out-of-stock|not-in-stock", status)
    image = html.unescape(g(r'<meta\s+property="og:image"\s+content="([^"]+)"'))
    desc = extract_div(page, 'id="tab_description"')
    desc = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", desc, flags=re.S | re.I)
    manuf = html.unescape(g(r'data-attribute-slug="MANUFACTURERS"[^>]*data-attribute-values=\'\["([^"]*)"'))
    cat = ""
    m = re.search(r'"@type":"BreadcrumbList".*?"itemListElement":(\[.*?\])\s*[},]', page, re.S)
    if m:
        try:
            names = [e["item"]["name"] for e in json.loads(m.group(1))]
            if len(names) >= 4:
                cat = names[-2]
        except Exception:  # noqa: BLE001
            pass
    return {"id": pid, "title": title, "price": price, "price_old": old, "in_stock": in_stock,
            "image": image, "desc_html": desc, "manufacturer": manuf, "cat_name": cat, "url": url}


def scrape_site():
    urls = product_urls()
    old_pages = load_json(PAGES_FILE, {})
    pages, errors = {}, 0
    for url in urls:
        try:
            rec = parse_page(fetch(url, timeout=60).decode("utf-8", "replace"), url)
            if rec["id"] and rec["title"]:
                pages[rec["id"]] = rec
            else:
                errors += 1
            time.sleep(0.25)
        except Exception as e:  # noqa: BLE001
            errors += 1
            print(f"  puslapis nenuskaitytas {url}: {e}")
            for pid, rec in old_pages.items():  # paliekam paskutini zinoma irasa
                if rec.get("url") == url:
                    pages[pid] = rec
    save_json(PAGES_FILE, pages)
    print(f"Svetaine: {len(urls)} URL, nuskaityta {len(pages)}, klaidu {errors}")
    return pages


# ----------------------------------------------------------------- Verskis kaina24 (kol veikia)

def load_kaina24():
    if not KAINA24_URL:
        return {}
    try:
        root = ET.fromstring(fetch(KAINA24_URL))
    except Exception as e:  # noqa: BLE001
        print(f"kaina24 XML nepasiekiamas ({e}) - naudojamas data/catalog.json")
        return {}
    items = {}
    for p in root.findall("product"):
        g = lambda tag, p=p: (p.findtext(tag) or "").strip()
        items[p.get("id")] = {"title": g("title"), "price": g("price"), "stock": g("stock"),
                              "ean": g("ean_code"), "code": g("manufacturer_code"),
                              "manufacturer": g("manufacturer"), "image": g("image_url"),
                              "url": g("product_url"), "cat_name": g("category_name"), "cat_id": g("category_id")}
    print(f"kaina24 XML: {len(items)} prekiu")
    return items


def cat_id_for(name, catalog_cats):
    if name in catalog_cats:
        return catalog_cats[name]
    return str(1000 + sum(ord(c) for c in name) % 9000)  # stabilus ID be Verskis


# ----------------------------------------------------------------- normalizavimas

def normalize(pages, kaina, catalog):
    """Sujungia svetaines duomenis su kaina24/katalogu. Grazina (products, skipped, catalog)."""
    cats = {v["cat_name"]: v["cat_id"] for v in catalog.values() if v.get("cat_name") and v.get("cat_id")}
    # atnaujinam kataloga is kaina24, kol jis veikia
    for pid, k in kaina.items():
        catalog[pid] = {"ean": re.sub(r"\D", "", k["ean"]), "code": k["code"], "cat_id": k["cat_id"],
                        "cat_name": k["cat_name"], "manufacturer": k["manufacturer"], "url": k["url"]}
        if k["cat_name"] and k["cat_id"]:
            cats[k["cat_name"]] = k["cat_id"]

    ids = set(pages) | set(kaina)
    products, skipped = [], []
    for pid in sorted(ids, key=lambda x: int(x) if x.isdigit() else 0):
        pg, k, c = pages.get(pid, {}), kaina.get(pid, {}), catalog.get(pid, {})
        title_raw = k.get("title") or pg.get("title") or ""
        if any(title_raw.strip().lower().startswith(p) for p in EXCLUDE_TITLE_PREFIXES):
            skipped.append((pid, title_raw, "ispardavimas (EXCLUDE_TITLE_PREFIXES)"))
            continue
        ean = re.sub(r"\D", "", k.get("ean") or c.get("ean") or "")
        if len(ean) < 8:
            skipped.append((pid, title_raw, "nera barkodo (papildyti data/catalog.json)"))
            continue
        price = float(k["price"]) if k.get("price") else pg.get("price")
        if not price or price <= 0:
            skipped.append((pid, title_raw, "nera kainos"))
            continue
        if k.get("stock"):
            qty = max(int(float(k["stock"])), 0)
        else:
            qty = DEFAULT_QTY if pg.get("in_stock") else 0
        cat_name = k.get("cat_name") or pg.get("cat_name") or c.get("cat_name") or "Maisto papildai"
        cat_id = k.get("cat_id") or c.get("cat_id") or cat_id_for(cat_name, cats)
        price_old = pg.get("price_old")
        price_old = price_old if price_old and price_old > price + 0.009 else None
        desc_html = (pg.get("desc_html") or "").strip() or "<p>" + html.escape(title_raw) + "</p>"
        products.append({
            "id": pid, "title": nice_title(title_raw), "desc_html": desc_html,
            "desc_text": strip_tags(desc_html)[:4900], "price": price, "price_old": price_old, "qty": qty,
            "ean": ean, "code": k.get("code") or c.get("code") or "",
            "manufacturer": k.get("manufacturer") or pg.get("manufacturer") or c.get("manufacturer") or "",
            "images": [u for u in [k.get("image") or pg.get("image")] if u],
            "url": k.get("url") or pg.get("url") or c.get("url") or "",
            "cat_id": cat_id, "cat_name": cat_name})
    return products, skipped, catalog


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
           "<title>citrina.eu</title>", f"<link>{SITE}/</link>",
           f"<description>citrina.eu prekiu feed'as Google Merchant Center, generated {now}</description>"]
    for p in products:
        out += ["<item>", f"<g:id>{e(p['id'])}</g:id>", f"<title>{e(p['title'])}</title>",
                f"<description>{e(p['desc_text'])}</description>", f"<link>{e(p['url'])}</link>"]
        if p["images"]:
            out.append(f"<g:image_link>{e(p['images'][0])}</g:image_link>")
            out += [f"<g:additional_image_link>{e(u)}</g:additional_image_link>" for u in p["images"][1:10]]
        out += [f"<g:availability>{'in_stock' if p['qty'] > 0 else 'out_of_stock'}</g:availability>",
                f"<g:price>{(p['price_old'] or p['price']):.2f} EUR</g:price>"]
        if p["price_old"]:
            out.append(f"<g:sale_price>{p['price']:.2f} EUR</g:sale_price>")
        out += [f"<g:brand>{e(brand_of(p['manufacturer']))}</g:brand>", f"<g:gtin>{p['ean']}</g:gtin>"]
        if p["code"]:
            out.append(f"<g:mpn>{e(p['code'])}</g:mpn>")
        out += ["<g:condition>new</g:condition>",
                f"<g:product_type>{e('Prekių katalogas > ' + p['cat_name'])}</g:product_type>",
                f"<g:google_product_category>{GOOGLE_CATEGORY}</g:google_product_category>", "</item>"]
    out += ["</channel>", "</rss>", ""]
    return "\n".join(out)


# ----------------------------------------------------------------- kaina24.lt / hind.ee

def write_kaina24(products, now):
    out = ['<?xml version="1.0" encoding="UTF-8"?>', f"<!-- citrina.eu -> kaina24.lt feed, generated {now} -->",
           "<products>"]
    for p in products:
        out += [f'<product id="{p["id"]}">', f"<title>{cdata(p['title'])}</title>",
                f"<price>{p['price']:.2f}</price>", f"<stock>{p['qty']}</stock>",
                f"<ean_code>{p['ean']}</ean_code>", f"<manufacturer_code>{html.escape(p['code'])}</manufacturer_code>",
                f"<manufacturer>{cdata(p['manufacturer'])}</manufacturer>",
                f"<image_url>{cdata(p['images'][0] if p['images'] else '')}</image_url>",
                f"<product_url>{cdata(p['url'])}</product_url>", f"<purchase_url>{cdata(p['url'])}</purchase_url>",
                f"<category_name>{cdata(p['cat_name'])}</category_name>",
                f"<category_id>{cdata(p['cat_id'])}</category_id>", "</product>"]
    out += ["</products>", ""]
    return "\n".join(out)


# ----------------------------------------------------------------- main

def main():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    catalog = load_json(CATALOG_FILE, {})
    kaina = load_kaina24()
    pages = scrape_site() if SCRAPE else load_json(PAGES_FILE, {})
    if not pages and not kaina:
        sys.exit("Nei svetaine, nei kaina24 XML nepasiekiami - feed'ai neatnaujinti")
    products, skipped, catalog = normalize(pages, kaina, catalog)
    save_json(CATALOG_FILE, catalog)

    os.makedirs(OUT_DIR, exist_ok=True)
    for name, text in (("varle.xml", write_varle(products, now)), ("google.xml", write_google(products, now)),
                       ("kaina24.xml", write_kaina24(products, now))):
        ET.fromstring(text.encode("utf-8"))
        with open(os.path.join(OUT_DIR, name), "w", encoding="utf-8") as f:
            f.write(text)
        print(f"OK: {name} - {len(products)} prekiu")
    print(f"Rezimas: {'kaina24 + svetaine' if kaina else 'tik svetaine (catalog.json)'}; "
          f"su sena kaina: {sum(1 for p in products if p['price_old'])}; praleista {len(skipped)}")
    for s in skipped:
        print("  praleista:", s)


if __name__ == "__main__":
    main()
