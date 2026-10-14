# citrina.eu → varle.lt XML feed

Automatiškai (kas 6 val.) sugeneruojamas Varle.lt marketplace XML iš Verskis.lt eksportų.

Feed'o adresas: `https://ginutis-hash.github.io/varle-feed/varle.xml`

- `build_feed.py` – generatorius
- `.github/workflows/build.yml` – planinis vykdymas (GitHub Actions)
- `docs/varle.xml` – rezultatas (publikuojamas per GitHub Pages iš `/docs`)

Nustatymai: Settings → Secrets and variables → Actions → `KAINA24_URL`, `GOOGLE_TSV_URL`.
