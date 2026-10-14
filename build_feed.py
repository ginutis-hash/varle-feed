#!/usr/bin/env python3
"""
Varle.lt marketplace XML generatorius citrina.eu parduotuvei.

Sujungia du Verskis.lt eksportus:
  * kaina24.lt XML      -> id, pavadinimas, kaina, likutis, EAN, kodas, gamintojas, nuotrauka, URL, kategorija
  * Google Shopping TSV -> aprasymas, sena kaina (price_old), papildomos nuotraukos
ir sugeneruoja Varle.lt XML (pagal sablona varle_xml_mp_(1a_1)).

Konfiguracija per aplinkos kintamuosius:
  KAINA24_URL, GOOGLE_TSV_URL  (privalomi; GitHub Secrets)
  DELIVERY_TEXT                 (numatyta "1-3 d. d.")
  OUTPUT                        (numatyta docs/varle.xml)
"""
import csv
import html
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

KAINA24_URL = os.environ.get("KAINA24_URL")
GOOGLE_TSV_URL = os.environ.get("GOOGLE_TSV_URL")
DELIVERY_TEXT = os.environ.get("DELIVERY_TEXT", "1-3 d. d.")
OUTPUT = os.environ.get("OUTPUT", "docs/varle.xml")

UPPER_TOKENS = {"JAV", "USA", "EU", "ES", "DHA", "EPA", "MSM", "CLA", "ALA", "NAC",
                "B12", "B6", "D3", "K2", "C", "E", "A", "D", "Q10", "CoQ10"}
UNITS = {"MG", "G", "MCG", "ML", "IU", "TV", "KAPS", "TAB", "KAPS.", "TAB.", "VNT", "VNT."}


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "citrina-varle-feed/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def nice_title(t: str) -> str:
    """BLACK COHOSH 540MG 60 KAPS. (SWANSON, JAV) -> Black Cohosh 540mg 60 kaps. (Swanson, JAV)"""
    t = re.sub(r"\s+", " ", t.strip())
    if t != t.upper():
        return t  # jau ne vien didziosios - paliekam
    out = []
    for w in t.split(" "):
        core = w.strip("(),.;:")
        if core in UPPER_TOKENS:
            out.append(w)
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
    """'10,95 EUR' -> 10.95"""
    if not s:
        return None
    m = re.search(r"[\d.,]+", s.replace(" ", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except ValueError:
        return None


def load_kaina24(data: bytes):
    root = ET.fromstring(data)
    items = {}
    for p in root.findall("product"):
        pid = p.get("id")

        def g(tag, p=p):
            return (p.findtext(tag) or "").strip()

        items[pid] = {
            "id": pid,
            "title": g("title"),
            "price": g("price"),
            "stock": g("stock"),
            "ean": g("ean_code"),
            "code": g("manufacturer_code"),
            "manufacturer": g("manufacturer"),
            "image": g("image_url"),
            "url": g("product_url"),
            "cat_name": g("category_name"),
            "cat_id": g("category_id"),
        }
    return items


def load_google_tsv(data: bytes):
    text = data.decode("utf-8-sig")
    reader = csv.DictReader(text.splitlines(), delimiter="\t", quotechar='"')
    rows = {}
    for r in reader:
        if r.get("id"):
            rows[r["id"].strip()] = r
    return rows


def cdata(s: str) -> str:
    return "<![CDATA[" + (s or "").replace("]]>", "]]]]><![CDATA[>") + "]]>"


def build(kaina, google):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    cats = {}
    for it in kaina.values():
        if it["cat_id"]:
            cats[it["cat_id"]] = it["cat_name"]

    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           f"<!-- citrina.eu -> varle.lt feed, generated {now} -->",
           "<root>", "  <categories>"]
    for cid, name in sorted(cats.items(), key=lambda x: x[1]):
        out += ["    <category>", f"      <id>{cid}</id>", "      <parent></parent>",
                f"      <name>{cdata(name)}</name>", "    </category>"]
    out.append("  </categories>")
    out.append("  <products>")

    skipped = []
    for pid, it in kaina.items():
        g = google.get(pid, {})
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

        desc = (g.get("description") or "").strip() or it["title"]
        desc_html = "<p>" + html.escape(desc) + "</p>"  # Verskis TSV aprasymas - grynas tekstas

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

        out.append("    <product>")
        out.append(f"      <id>{pid}</id>")
        out.append("      <categories>")
        out.append(f"        <category>{it['cat_id']}</category>")
        out.append("      </categories>")
        out.append(f"      <title>{cdata(nice_title(it['title']))}</title>")
        out.append(f"      <description>{cdata(desc_html)}</description>")
        out.append(f"      <price>{price:.2f}</price>")
        if price_old:
            out.append(f"      <price_old>{price_old:.2f}</price_old>")
        out.append(f"      <delivery_text>{DELIVERY_TEXT}</delivery_text>")
        out.append("      <images>")
        for u in images:
            out.append(f"        <image>{cdata(u)}</image>")
        out.append("      </images>")
        out.append(f"      <quantity>{qty}</quantity>")
        out.append(f"      <barcode_format>{'UPC' if len(ean) == 12 else 'EAN'}</barcode_format>")
        out.append(f"      <barcode>{ean}</barcode>")
        if it["code"]:
            out.append(f"      <model>{cdata(it['code'])}</model>")
        if it["manufacturer"]:
            out.append(f"      <manufacturer>{cdata(it['manufacturer'])}</manufacturer>")
        if it["url"]:
            out.append(f"      <url>{cdata(it['url'])}</url>")
        out.append("      <product_with_gift>False</product_with_gift>")
        out.append("    </product>")

    out += ["  </products>", "</root>", ""]
    return "\n".join(out), skipped


def main():
    if not KAINA24_URL or not GOOGLE_TSV_URL:
        sys.exit("Truksta KAINA24_URL arba GOOGLE_TSV_URL aplinkos kintamuju")
    kaina = load_kaina24(fetch(KAINA24_URL))
    google = load_google_tsv(fetch(GOOGLE_TSV_URL))
    xml_text, skipped = build(kaina, google)
    ET.fromstring(xml_text.encode("utf-8"))  # validumo patikra
    os.makedirs(os.path.dirname(OUTPUT) or ".", exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(xml_text)
    print(f"OK: {xml_text.count('<product>')} prekiu irasyta i {OUTPUT}; praleista {len(skipped)}")
    for s in skipped:
        print("  praleista:", s)


if __name__ == "__main__":
    main()
