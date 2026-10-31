# Analisis Sentimen Ulasan Sneakers Tokopedia

Proyek Analisis Sentimen (Submission Dicoding). Topik yang dianalisis adalah ulasan produk **sepatu sneakers** di Tokopedia dengan 3 kelas sentimen (`negatif`, `netral`, `positif`).

## Isi Folder Submission (`submission_sentimen_tokopedia/`)

| Berkas | Fungsi | Status |
|---|---|---|
| `scraping_tokopedia.py` | Kode scraping mandiri (Python + Playwright) | Disertakan |
| `notebook_pelatihan_sentimen.ipynb` | Preprocessing, pelabelan hybrid, 3 skema deep learning, evaluasi, inference | **Sudah dieksekusi penuh, seluruh output & visualisasi tersimpan** |
| `requirements.txt` | Daftar pustaka Python beserta versi yang digunakan | Sesuai lingkungan eksekusi |
| `dataset_tokopedia.csv` | Dataset mentah hasil scraping mandiri (40.343 ulasan sneakers) | Lengkap |
| `dataset_berlabel.csv` | Dataset bersih berlabel untuk pelatihan (11.400 sampel) | Lengkap |
| `hasil_eksperimen.csv` | Tabel ringkasan metrik akurasi ketiga skema pelatihan | Lengkap |
| `README_PANDUAN.md` | Dokumentasi dan panduan reproduksi proyek | Lengkap |

---

## Ringkasan Eksperimen & Hasil Pelatihan

Ketiga skema menggunakan representasi input **WordPiece Subword Tokenizer** (`indobenchmark/indobert-base-p1`). Dua variabel yang divariasikan antar skema sesuai ketentuan Dicoding adalah **Algoritma/Arsitektur Model** dan **Proporsi Pembagian Data (Split)**:

| Skema | Algoritma / Arsitektur | Tokenisasi & Representasi Fitur | Pembagian Data | Akurasi Train | Akurasi Test |
|---|---|---|---|---|---|
| **Skema 1** | IndoBERT Base (`SequenceClassification`) | WordPiece Subword (`IndoBERT`) | Split 80/20 | 99,26% | 94,69% |
| **Skema 2** | IndoBERT Base (`SequenceClassification`) | WordPiece Subword (`IndoBERT`) | Split 70/30 | 97,86% | 94,21% |
| **Skema 3** | IndoBERT-CNN Hybrid (Backbone + Multi-Kernel Conv1D) | WordPiece Subword + Conv1D Head | Split 80/20 | 99,23% | 94,34% |

* **Alur Data (40.343 $\rightarrow$ 11.400 sampel)**:
  1. Data mentah hasil scraping: **40.343 baris**
  2. Penyaringan produk non-sepatu (2.202) & baris teks kosong (1): **-2.203 baris** (sisa 38.140 baris)
  3. Filter ulasan terlalu pendek ($< 2$ kata bersih): **-1.922 baris** (sisa 36.218 baris)
  4. Pembuangan konflik rating vs polaritas teks: **-7.389 baris** (sisa 28.829 baris)
  5. Deduplikasi teks bersih identik: **-1.757 baris** (sisa 27.072 baris)
  6. *Undersampling* penyeimbangan kelas: **-15.672 baris**
  7. **Dataset final pelatihan**: **11.400 sampel** (`positif`: 6.400, `negatif`: 2.900, `netral`: 2.100).
* **Strategi Evaluasi**: *Stratified split* menjadi *Train*, *Validation* (10% dari bagian *Train* untuk *best-checkpoint selection*), dan *Test set* terpisah untuk evaluasi akhir.

---

## Panduan Reproduksi Lingkungan

### 1. Persiapan Lingkungan Virtual
```bash
python -m venv .venv
.venv\Scripts\activate            # Windows (Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt
python -m playwright install chromium
```

### 2. Menjalankan Scraping Data
```bash
python scraping_tokopedia.py discover --pages 6           # kumpulkan URL produk -> produk_urls.csv
python scraping_tokopedia.py reviews --max-products 400   # ambil ulasan produk  -> dataset_tokopedia.csv
python scraping_tokopedia.py stats                        # ringkasan distribusi rating
```
* Proses *scraping* dilengkapi jeda acak antar-permintaan dan dapat dihentikan (`Ctrl+C`) serta dilanjutkan kembali (*resume*) karena daftar produk yang telah selesai dicatat pada `produk_selesai.txt`.

### 3. Menjalankan Notebook Pelatihan
```bash
jupyter notebook notebook_pelatihan_sentimen.ipynb
```
> **Catatan untuk Reviewer**: Notebook `notebook_pelatihan_sentimen.ipynb` di dalam berkas submission ini **sudah dijalankan secara penuh** sehingga seluruh output teks, grafik kurva *loss*/akurasi, *confusion matrix*, *classification report*, dan hasil *inference* dapat langsung diperiksa tanpa perlu menjalankan ulang pelatihan.

---

## Etika & Kepatuhan Data
- Hanya mengambil ulasan produk yang tampil secara publik tanpa autentikasi/login.
- Identitas atau nama akun pembeli **tidak disimpan** di dalam dataset (hanya ID ulasan, nama/URL produk, rating bintang, teks ulasan, dan tanggal).
- Topik ulasan produk sepatu sneakers bersifat umum dan tidak memuat unsur sensitif atau kontroversial.
