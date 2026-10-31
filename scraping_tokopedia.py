"""
Scraping ulasan produk Tokopedia - kategori "sepatu sneakers"
================================================================
Dipakai untuk submission Proyek Analisis Sentimen (Dicoding).

Cara kerja singkat
------------------
1. `discover`  : membuka halaman pencarian Tokopedia (diurutkan berdasarkan ulasan),
                 mengumpulkan URL produk  -> produk_urls.csv
2. `reviews`   : membuka halaman ulasan tiap produk, MENANGKAP request GraphQL ulasan
                 yang dikirim browser itu sendiri, lalu mengulangnya dengan variabel
                 yang diubah (filter rating 1..5 dan nomor halaman).
                 Kalau pengulangan request gagal (mis. diblokir), otomatis pindah ke
                 mode UI (klik "Laman berikutnya" sambil menyadap response).
                 -> dataset_tokopedia.csv (disimpan bertahap, bisa dilanjutkan/resume)
3. `stats`     : ringkasan jumlah data per rating.

Contoh pemakaian
----------------
    python -m playwright install chromium
    python scraping_tokopedia.py discover --pages 6
    python scraping_tokopedia.py reviews --max-products 400
    python scraping_tokopedia.py stats

Catatan etika & kepatuhan
-------------------------
* Hanya mengambil ulasan yang tampil publik tanpa login.
* Nama/identitas pengulas TIDAK disimpan (hanya teks, rating, tanggal).
* Ada jeda acak antar request. Jangan menurunkan jeda agar tidak membebani server.
* Periksa Syarat & Ketentuan Tokopedia sebelum scraping, dan gunakan data hanya untuk
  keperluan belajar/penelitian.

Catatan teknis
--------------
Struktur GraphQL Tokopedia bisa berubah sewaktu-waktu. Kode ini sengaja TIDAK
menyalin query secara permanen (query ditangkap langsung dari browser) dan parser
ulasannya mencari field secara rekursif, agar tahan terhadap perubahan kecil.
Jika tetap gagal, lihat bagian "Troubleshooting" pada README_PANDUAN.md.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import pandas as pd

# --------------------------------------------------------------------------- #
# Konfigurasi
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent
PRODUCT_CSV = BASE_DIR / "produk_urls.csv"
DATASET_CSV = BASE_DIR / "dataset_tokopedia.csv"
DONE_TXT = BASE_DIR / "produk_selesai.txt"
PROFILE_DIR = BASE_DIR / ".browser_profile"  # menyimpan sesi browser (jangan di-zip)

DEFAULT_KEYWORDS = [
    "sepatu sneakers pria",
    "sepatu sneakers wanita",
    "sepatu sneakers putih",
    "sepatu sneakers hitam",
    "sepatu sneakers casual",
    "sneakers import",
    "sepatu olahraga sneakers",
    "sneakers unisex",
]

# Batas ulasan yang diambil per produk untuk tiap rating.
# Rating 1-3 dibuat lebih besar karena kelas negatif & netral jauh lebih jarang.
DEFAULT_CAPS = {1: 200, 2: 200, 3: 300, 4: 150, 5: 150}

RESERVED_SLUGS = {
    "search", "discovery", "find", "p", "blog", "help", "bantuan", "promo", "about",
    "cart", "login", "register", "mitra", "official-store", "hot", "deals", "events",
    "play", "pulsa", "emas", "tiket", "kategori", "terms", "privacy", "careers",
    "mobile-apps", "feed", "wishlist", "order-list", "digital", "top-ads", "ta",
}

CSV_COLUMNS = [
    "review_id", "product_name", "product_url", "rating",
    "review_text", "review_date", "scraped_at",
]

TEXT_KEYS = ("message", "reviewText", "review_text", "reviewMessage")
RATING_KEYS = ("productRating", "rating", "ratingValue", "reviewRating")
ID_KEYS = ("feedbackID", "feedbackId", "reviewID", "reviewId", "id")
TIME_KEYS = ("reviewCreateTime", "createTime", "createTimestamp", "reviewTime", "timestamp")


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def sleep_random(a: float = 0.8, b: float = 2.0) -> None:
    time.sleep(random.uniform(a, b))


# --------------------------------------------------------------------------- #
# Fungsi murni (mudah diuji tanpa browser)
# --------------------------------------------------------------------------- #
def normalize_product_url(href: str) -> str | None:
    """Mengubah href hasil pencarian menjadi URL produk bersih.

    * Link iklan (ta.tokopedia.com/promo/...?r=<url produk>) dibuka dari parameter `r`.
    * Hanya URL berbentuk https://www.tokopedia.com/<toko>/<produk> yang diterima.
    """
    if not href:
        return None
    parsed = urlparse(href)
    if parsed.netloc.startswith("ta.tokopedia.com"):
        target = parse_qs(parsed.query).get("r")
        if not target:
            return None
        parsed = urlparse(unquote(target[0]))
    if not parsed.netloc.endswith("tokopedia.com"):
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        return None
    shop, product = parts[0], parts[1]
    if shop.lower() in RESERVED_SLUGS or product.lower() in RESERVED_SLUGS:
        return None
    if product.lower() in {"review", "info", "etalase", "product", "produk"}:
        return None
    return f"https://www.tokopedia.com/{shop}/{product}"


def product_name_from_url(url: str) -> str:
    slug = url.rstrip("/").split("/")[-1]
    return re.sub(r"\s+", " ", slug.replace("-", " ")).strip()


def _first(d: dict, keys: tuple[str, ...]):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def iter_reviews(node):
    """Mencari objek ulasan secara rekursif di JSON response.

    Objek dianggap ulasan jika punya field teks (message/...) bertipe string DAN
    field rating bernilai 1-5. Balasan penjual (punya teks, tanpa rating) diabaikan.
    """
    if isinstance(node, dict):
        text = _first(node, TEXT_KEYS)
        rating = _first(node, RATING_KEYS)
        if isinstance(text, str):
            try:
                r = int(float(rating))
            except (TypeError, ValueError):
                r = None
            if r is not None and 1 <= r <= 5:
                yield {
                    "id": _first(node, ID_KEYS),
                    "text": text,
                    "rating": r,
                    "time": _first(node, TIME_KEYS),
                }
                return
        for v in node.values():
            yield from iter_reviews(v)
    elif isinstance(node, list):
        for v in node:
            yield from iter_reviews(v)


def find_key(node, key: str):
    """Mengembalikan nilai pertama untuk `key` di JSON bersarang (atau None)."""
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for v in node.values():
            found = find_key(v, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for v in node:
            found = find_key(v, key)
            if found is not None:
                return found
    return None


def format_time(value) -> str:
    """Epoch (detik/milidetik) atau string -> 'YYYY-MM-DD' bila memungkinkan."""
    if value is None:
        return ""
    try:
        num = float(value)
        if num > 1e11:  # milidetik
            num /= 1000.0
        if num > 1e8:
            return datetime.fromtimestamp(num, tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        pass
    return str(value)[:30]


def parse_caps(text: str | None) -> dict[int, int]:
    if not text:
        return dict(DEFAULT_CAPS)
    caps = dict(DEFAULT_CAPS)
    for chunk in text.split(","):
        k, v = chunk.split(":")
        caps[int(k)] = int(v)
    return caps


def make_row(rev: dict, product_url: str) -> dict:
    text = re.sub(r"\s+", " ", rev["text"]).strip()
    rid = rev["id"]
    if rid is None:
        digest = hashlib.md5(f"{product_url}|{text}|{rev['time']}".encode("utf-8")).hexdigest()
        rid = f"h{digest[:16]}"
    return {
        "review_id": str(rid),
        "product_name": product_name_from_url(product_url),
        "product_url": product_url,
        "rating": rev["rating"],
        "review_text": text,
        "review_date": format_time(rev["time"]),
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------------------- #
# Penyimpanan
# --------------------------------------------------------------------------- #
def load_seen_ids() -> set[str]:
    if not DATASET_CSV.exists():
        return set()
    try:
        return set(pd.read_csv(DATASET_CSV, usecols=["review_id"], dtype=str)["review_id"])
    except Exception:
        return set()


def append_rows(rows: list[dict]) -> None:
    if not rows:
        return
    new_file = not DATASET_CSV.exists()
    with open(DATASET_CSV, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if new_file:
            writer.writeheader()
        writer.writerows(rows)


def load_done() -> set[str]:
    if not DONE_TXT.exists():
        return set()
    return {line.strip() for line in DONE_TXT.read_text(encoding="utf-8").splitlines() if line.strip()}


def mark_done(url: str) -> None:
    with open(DONE_TXT, "a", encoding="utf-8") as f:
        f.write(url + "\n")


# --------------------------------------------------------------------------- #
# Browser
# --------------------------------------------------------------------------- #
def open_browser(playwright, headless: bool):
    context = playwright.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=headless,
        viewport={"width": 1366, "height": 850},
        locale="id-ID",
        timezone_id="Asia/Jakarta",
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = context.pages[0] if context.pages else context.new_page()
    return context, page


CHALLENGE_PHRASES = (
    "captcha", "verifikasi bahwa anda", "verify you are", "are you a robot", "unusual traffic",
    "akses ditolak", "access denied", "kami mendeteksi aktivitas",
)
EMPTY_PHRASES = ("produk nggak ditemukan", "produk tidak ditemukan", "produk ngga ditemukan")


def visible_text(page) -> str:
    try:
        return page.inner_text("body", timeout=4000).lower()
    except Exception:
        return ""


def has_challenge(text: str) -> bool:
    return any(k in text for k in CHALLENGE_PHRASES)


def is_empty_result(text: str) -> bool:
    return any(k in text for k in EMPTY_PHRASES)


def handle_challenge(page) -> None:
    """Jika benar-benar tampil captcha/verifikasi, minta pengguna menyelesaikannya manual.

    Pengecekan memakai teks yang TAMPIL di layar (bukan kode HTML), sehingga kata
    'captcha' di dalam skrip situs tidak lagi memicu peringatan palsu.
    """
    if has_challenge(visible_text(page)):
        input(">>> Terdeteksi captcha/verifikasi. Selesaikan di jendela browser, lalu tekan ENTER di sini...")


def goto_safe(page, url: str, wait_ms: int = 2500) -> bool:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(wait_ms)
        handle_challenge(page)
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"  gagal membuka {url}: {exc}")
        return False


# --------------------------------------------------------------------------- #
# Tahap 1: discover
# --------------------------------------------------------------------------- #
def cmd_discover(args) -> None:
    from playwright.sync_api import sync_playwright

    keywords = args.keywords or DEFAULT_KEYWORDS
    found: dict[str, str] = {}
    if PRODUCT_CSV.exists():
        old = pd.read_csv(PRODUCT_CSV)
        found = dict(zip(old["product_url"], old["keyword"]))
        log(f"melanjutkan, {len(found)} produk sudah tercatat")

    with sync_playwright() as pw:
        context, page = open_browser(pw, headless=args.headless)
        try:
            for kw in keywords:
                for p in range(1, args.pages + 1):
                    log(f"cari '{kw}' halaman {p}")
                    page_ok = False
                    # Coba urut ulasan (ob=5) dulu; jika kosong, coba tanpa parameter urutan.
                    for with_sort in (True, False):
                        url = f"https://www.tokopedia.com/search?q={quote_plus(kw)}&page={p}"
                        if with_sort:
                            url += "&ob=5"
                        if not goto_safe(page, url, wait_ms=3000):
                            continue
                        if is_empty_result(visible_text(page)):
                            page.wait_for_timeout(2500)  # beri waktu, lalu cek ulang
                            if is_empty_result(visible_text(page)):
                                log("  hasil kosong" + (" -> coba tanpa urutan" if with_sort else ""))
                                continue
                        page_ok = True
                        break
                    if not page_ok:
                        log(f"  tidak ada hasil lagi untuk '{kw}', lanjut keyword berikutnya")
                        break

                    for _ in range(10):  # scroll bertahap agar semua kartu produk termuat
                        page.mouse.wheel(0, 1200)
                        page.wait_for_timeout(500)
                    hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
                    on_page = {normalize_product_url(h) for h in hrefs} - {None}
                    new = 0
                    for u in on_page:
                        if u not in found:
                            found[u] = kw
                            new += 1
                    log(f"  {len(on_page)} produk di halaman, +{new} baru (total {len(found)})")
                    pd.DataFrame(
                        {"product_url": list(found), "keyword": list(found.values())}
                    ).to_csv(PRODUCT_CSV, index=False)
                    if not on_page:
                        break
                    sleep_random(1.5, 3.5)
        finally:
            context.close()
    log(f"selesai. {len(found)} URL produk tersimpan di {PRODUCT_CSV.name}")


# --------------------------------------------------------------------------- #
# Tahap 2: reviews
# --------------------------------------------------------------------------- #
CLEAN_HEADER_DROP = {"content-length", "host", "connection", "accept-encoding", "cookie"}
# header yang tidak boleh diset manual lewat fetch() di dalam halaman (diatur browser sendiri)
FETCH_FORBIDDEN = {"user-agent", "origin", "referer", "content-type-x"}
DEBUG = False
FILTER_FORMATS = ["rating={r}", "rating:{r}", "rating={r};", "star={r}", "{r}"]
FILTER_FMT: str | None = None   # format filter yang terbukti bekerja (di-cache antar produk)
DEBUG_FILE = BASE_DIR / "debug_capture.json"

PAGE_FETCH_JS = """
async ({url, headers, body}) => {
  try {
    const r = await fetch(url, {method: 'POST', headers: headers, body: body, credentials: 'include'});
    const t = await r.text();
    return {status: r.status, text: t};
  } catch (e) { return {status: 0, text: String(e)}; }
}
"""


def capture_review_request(page, product_url: str, timeout_s: int = 20):
    """Buka halaman ulasan & tangkap request GraphQL ulasan pertama dari browser."""
    captured: dict = {}
    seen_posts: list[str] = []

    def on_request(req):
        if req.method != "POST" or "tokopedia.com" not in req.url:
            return
        body = req.post_data or ""
        names = re.findall(r'"operationName"\s*:\s*"([^"]+)"', body)
        seen_posts.extend(names or [req.url[:80]])
        if captured:
            return
        low = body.lower()
        # Hanya request DAFTAR ulasan (mis. productrevGetProductReviewList),
        # bukan ringkasan rating (productrevGetProductRatingAndTopics).
        if "reviewlist" in low:
            captured["req"] = req

    page.on("request", on_request)
    try:
        if not goto_safe(page, product_url.rstrip("/") + "/review", wait_ms=1500):
            return None
        waited = 0.0
        while "req" not in captured and waited < timeout_s:
            page.mouse.wheel(0, 600)
            page.wait_for_timeout(700)
            waited += 0.7
    finally:
        page.remove_listener("request", on_request)
    req = captured.get("req")
    if req is None:
        log(f"  [alasan] request ReviewList TIDAK tertangkap. Operasi yang terlihat: {sorted(set(seen_posts))[:8]}")
        return None
    try:
        payload = json.loads(req.post_data)
    except Exception:
        log("  [alasan] body request ulasan bukan JSON")
        return None
    headers = {
        k: v for k, v in req.all_headers().items()
        if not k.startswith(":") and k.lower() not in CLEAN_HEADER_DROP
    }
    cap = {"url": req.url, "headers": headers, "payload": payload}
    if DEBUG:
        ops = payload if isinstance(payload, list) else [payload]
        DEBUG_FILE.write_text(json.dumps({
            "url": req.url,
            "header_names": sorted(headers.keys()),   # hanya NAMA header, tanpa nilai (aman dibagikan)
            "operations": [{
                "operationName": o.get("operationName"),
                "variables": o.get("variables"),
                "query_head": str(o.get("query", ""))[:200],
            } for o in ops],
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        log(f"  [debug] struktur request disimpan ke {DEBUG_FILE.name}")
    return cap


def _review_op_index(payload) -> int | None:
    """Indeks operasi DAFTAR ulasan di body GraphQL (body bisa berupa list)."""
    if isinstance(payload, dict):
        return None
    for i, op in enumerate(payload):
        blob = (str(op.get("operationName", "")) + str(op.get("query", ""))).lower()
        if "reviewlist" in blob:
            return i
    return 0


def _mutate_payload(payload, rating: int | None, page_no: int, limit: int | None, fmt: str | None = None):
    body = json.loads(json.dumps(payload))  # deep copy
    idx = _review_op_index(body)
    op = body if idx is None else body[idx]
    variables = op.setdefault("variables", {})
    variables["page"] = page_no
    if limit and "limit" in variables:
        variables["limit"] = limit
    if rating is not None and fmt and "filterBy" in variables:
        variables["filterBy"] = fmt.format(r=rating)
    return body


def detect_filter_format(post, cap: dict, limit: int) -> str | None:
    """Mencoba beberapa format filter rating; pilih yang hasilnya HANYA berisi bintang yang diminta."""
    for r in (1, 2, 3):
        for fmt in FILTER_FORMATS:
            body = _mutate_payload(cap["payload"], r, 1, limit, fmt)
            try:
                status, text = post(cap, json.dumps(body))
                data = json.loads(text) if status == 200 else None
            except Exception:  # noqa: BLE001
                continue
            if data is None:
                continue
            revs = list(iter_reviews(data))
            if revs and all(x["rating"] == r for x in revs):
                log(f"  [info] format filter rating yang bekerja: {fmt!r}")
                return fmt
            sleep_random(0.3, 0.7)
    return None


def replay_reviews(post, cap: dict, caps: dict[int, int], limit: int, max_pages: int,
                   seen: set[str], product_url: str) -> tuple[list[dict], bool]:
    """Mengambil ulasan per rating dengan mengulang request hasil tangkapan.

    `post(cap, body_str) -> (status, text)` adalah fungsi pengirim request.
    Mengembalikan (baris_baru, berhasil). `berhasil=False` -> coba cara lain / mode UI.
    """
    rows: list[dict] = []
    has_filter = "filterBy" in json.dumps(cap["payload"])
    if not has_filter:
        log("  [info] variabel 'filterBy' tidak ada di request -> filter rating tidak bisa dipakai")
    ratings: list[int | None] = [1, 2, 3, 4, 5] if has_filter else [None]
    fmt = None
    if has_filter:
        global FILTER_FMT
        fmt = FILTER_FMT or detect_filter_format(post, cap, limit)
        if fmt is None:
            log("  [info] format filter rating tidak ketemu -> ambil tanpa filter")
            ratings = [None]
        else:
            FILTER_FMT = fmt
    got_any = False
    filter_broken = False

    for rating in ratings:
        quota = caps.get(rating, 150) if rating else sum(caps.values())
        collected = 0
        for page_no in range(1, max_pages + 1):
            body = _mutate_payload(cap["payload"], rating, page_no, limit, fmt)
            try:
                status, text = post(cap, json.dumps(body))
            except Exception as exc:  # noqa: BLE001
                log(f"  [alasan] error saat kirim request: {exc}")
                break
            if status != 200:
                log(f"  [alasan] status {status} (rating={rating}, halaman={page_no}) isi: {text[:200]!r}")
                break
            try:
                data = json.loads(text)
            except Exception:
                log(f"  [alasan] response bukan JSON: {text[:200]!r}")
                break
            revs = list(iter_reviews(data))
            if not revs:
                if page_no == 1:
                    log(f"  [alasan] 0 ulasan terbaca (rating={rating}). Awal response: {text[:300]!r}")
                break
            got_any = True
            if rating is not None and page_no == 1 and any(r["rating"] != rating for r in revs):
                log("  filter rating tidak bekerja -> ambil tanpa filter")
                filter_broken = True
            for r in revs:
                row = make_row(r, product_url)
                if row["review_id"] in seen or not row["review_text"]:
                    continue
                seen.add(row["review_id"])
                rows.append(row)
                collected += 1
            if collected >= quota or find_key(data, "hasNext") is False:
                break
            sleep_random(0.6, 1.4)
        if filter_broken:
            break
        sleep_random(0.4, 1.0)
    return rows, got_any


def ui_collect(page, product_url: str, seen: set[str], max_pages: int) -> list[dict]:
    """Mode cadangan: menyadap response GraphQL sambil klik halaman berikutnya."""
    rows: list[dict] = []

    def on_response(resp):
        if "gql.tokopedia.com" not in resp.url:
            return
        try:
            data = resp.json()
        except Exception:
            return
        for r in iter_reviews(data):
            row = make_row(r, product_url)
            if row["review_id"] in seen or not row["review_text"]:
                continue
            seen.add(row["review_id"])
            rows.append(row)

    page.on("response", on_response)
    try:
        if not goto_safe(page, product_url.rstrip("/") + "/review", wait_ms=2500):
            return rows
        for _ in range(max_pages):
            nxt = page.locator('button[aria-label="Laman berikutnya"]')
            if nxt.count() == 0 or not nxt.first.is_enabled():
                break
            try:
                nxt.first.click(timeout=5000)
            except Exception:
                break
            page.wait_for_timeout(int(random.uniform(1200, 2200)))
    finally:
        page.remove_listener("response", on_response)
    return rows


def cmd_reviews(args) -> None:
    global DEBUG
    from playwright.sync_api import sync_playwright

    DEBUG = args.debug
    if not PRODUCT_CSV.exists():
        sys.exit("produk_urls.csv belum ada. Jalankan dulu: python scraping_tokopedia.py discover")
    urls = pd.read_csv(PRODUCT_CSV)["product_url"].tolist()
    done = load_done()
    todo = [u for u in urls if u not in done][: args.max_products]
    caps = parse_caps(args.caps)
    seen = load_seen_ids()
    log(f"{len(todo)} produk akan diproses | ulasan tersimpan: {len(seen)}")

    with sync_playwright() as pw:
        context, page = open_browser(pw, headless=args.headless)

        def post_ctx(cap, body_str):
            resp = context.request.post(cap["url"], headers=cap["headers"], data=body_str, timeout=30000)
            return resp.status, resp.text()

        def post_page(cap, body_str):
            hdrs = {k: v for k, v in cap["headers"].items()
                    if not k.lower().startswith("sec-") and k.lower() not in FETCH_FORBIDDEN}
            res = page.evaluate(PAGE_FETCH_JS, {"url": cap["url"], "headers": hdrs, "body": body_str})
            return res["status"], res["text"]

        try:
            for i, url in enumerate(todo, 1):
                log(f"[{i}/{len(todo)}] {product_name_from_url(url)[:70]}")
                cap = capture_review_request(page, url)
                rows: list[dict] = []
                ok = False
                if cap is not None:
                    for name, post in (("request", post_ctx), ("fetch-halaman", post_page)):
                        rows, ok = replay_reviews(post, cap, caps, args.limit, args.max_pages, seen, url)
                        if ok:
                            log(f"  replay berhasil via {name}")
                            break
                        log(f"  replay via {name} gagal")
                if not ok:
                    log("  -> pakai mode UI (tanpa filter rating)")
                    rows = ui_collect(page, url, seen, args.max_pages)
                append_rows(rows)
                mark_done(url)
                dist = pd.Series([r["rating"] for r in rows], dtype="int64").value_counts().sort_index().to_dict()
                log(f"  +{len(rows)} ulasan {dist} | total {len(seen)}")
                if args.target and len(seen) >= args.target:
                    log("target jumlah ulasan tercapai.")
                    break
                sleep_random(2.0, 4.5)
        finally:
            context.close()


# --------------------------------------------------------------------------- #
# Tahap 3: stats
# --------------------------------------------------------------------------- #
def cmd_stats(_args) -> None:
    if not DATASET_CSV.exists():
        sys.exit("dataset_tokopedia.csv belum ada.")
    df = pd.read_csv(DATASET_CSV)
    print(f"Total ulasan      : {len(df)}")
    print(f"Produk unik       : {df['product_url'].nunique()}")
    print("Distribusi rating :")
    print(df["rating"].value_counts().sort_index().to_string())
    lab = df["rating"].map(lambda r: "negatif" if r <= 2 else "netral" if r == 3 else "positif")
    print("Distribusi kelas (berdasarkan rating):")
    print(lab.value_counts().to_string())


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Scraper ulasan sneakers Tokopedia")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="kumpulkan URL produk")
    d.add_argument("--pages", type=int, default=6, help="jumlah halaman pencarian per keyword")
    d.add_argument("--keywords", nargs="*", help="override keyword pencarian")
    d.add_argument("--headless", action="store_true", help="tanpa jendela browser (tidak disarankan)")
    d.set_defaults(func=cmd_discover)

    r = sub.add_parser("reviews", help="ambil ulasan tiap produk")
    r.add_argument("--max-products", type=int, default=400)
    r.add_argument("--max-pages", type=int, default=15, help="maks halaman per rating per produk")
    r.add_argument("--limit", type=int, default=40, help="ulasan per request (turunkan ke 10 bila error)")
    r.add_argument("--caps", help='batas per rating per produk, mis. "1:200,2:200,3:300,4:150,5:150"')
    r.add_argument("--target", type=int, default=30000, help="berhenti jika total ulasan mencapai angka ini")
    r.add_argument("--headless", action="store_true")
    r.add_argument("--debug", action="store_true", help="simpan struktur request ke debug_capture.json")
    r.set_defaults(func=cmd_reviews)

    s = sub.add_parser("stats", help="ringkasan dataset")
    s.set_defaults(func=cmd_stats)
    return ap


if __name__ == "__main__":
    parsed = build_parser().parse_args()
    parsed.func(parsed)
