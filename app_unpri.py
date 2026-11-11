"""
Lost & Found Unpri - aplikasi web (Streamlit + Ollama + SQLite)

Persiapan:  pip install streamlit ollama   |   ollama pull llama3.2
Jalankan :  streamlit run app_unpri.py
Admin    :  password dari variabel lingkungan ADMIN_PASSWORD (default: admin123)
"""
import os
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import ollama
import pandas as pd
import streamlit as st

DB_FILE = "unpri_lostfound.db"
FOTO_DIR = Path("foto_barang")
FOTO_DIR.mkdir(exist_ok=True)
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
KATEGORI = ["Dompet & Tas", "Elektronik", "Kunci", "Kartu & Dokumen", "Buku & Alat Tulis",
            "Pakaian & Aksesori", "Botol & Tumbler", "Lainnya"]
LOKASI = ["Perpustakaan", "Kantin", "Parkiran", "Ruang kelas", "Laboratorium", "Musala/Masjid",
          "Lapangan", "Toilet", "Gedung lain / tidak yakin"]

SYSTEM_PROMPT = f"""Kamu adalah AI Asisten Lost and Found Unpri (Universitas Prima Indonesia).
Gunakan bahasa Indonesia yang ramah dan singkat. Kategori barang: {", ".join(KATEGORI)}.

Skenario yang kamu tangani:
1. Pengguna KEHILANGAN barang: kumpulkan nama, kategori, ciri-ciri, lokasi, kontak, lalu panggil lapor_barang (status hilang).
2. Pengguna MENEMUKAN barang: sama, status ditemukan. Sarankan juga menitipkan barang ke petugas kampus.
3. Pengguna MENCARI barang: panggil cari_barang. Kata kunci kosong = laporan terbaru.
4. Pengguna mengaku PEMILIK barang yang ditemukan: minta ciri khusus yang hanya pemilik tahu, lalu panggil ajukan_tanggapan.
5. Pengguna MENEMUKAN barang yang sedang dicari orang: panggil ajukan_tanggapan dengan keterangan tempat dan kondisi barang.
6. Pengguna menanyakan STATUS klaim: minta kontak yang dipakai, lalu panggil cek_tanggapan.
7. Pengguna ingin MENUTUP laporannya: minta ID dan PIN 4 digit, lalu panggil tutup_laporan.
8. Pengguna menanyakan statistik: panggil statistik.
9. Di luar topik Lost and Found: tolak dengan sopan dan arahkan kembali.

Aturan:
- JANGAN pernah menolak urusan barang hilang atau ditemukan. Jika data kurang, TANYAKAN data yang kurang dalam satu pesan. Jangan mengarang.
- Setelah lapor_barang berhasil, sampaikan ID laporan dan PIN, dan minta pengguna menyimpan PIN. Sebutkan kandidat_cocok jika ada.
- JANGAN bilang sudah tersimpan kecuali tool berhasil dipanggil. Jawab hanya berdasarkan hasil tool.
- Kontak orang lain hanya boleh disampaikan jika hasil cek_tanggapan memuat kontak_pelapor.
- Jangan meminta data sensitif (NIK, password, nomor rekening)."""


# ---------- Database ----------
def _con():
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    con.executescript("""
    CREATE TABLE IF NOT EXISTS barang(
        id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT, nama TEXT, kategori TEXT,
        deskripsi TEXT, lokasi TEXT, tanggal TEXT, kontak TEXT, pin TEXT, foto TEXT,
        selesai INTEGER DEFAULT 0, catatan TEXT, dibuat TEXT);
    CREATE TABLE IF NOT EXISTS tanggapan(
        id INTEGER PRIMARY KEY AUTOINCREMENT, barang_id INTEGER, nama TEXT, kontak TEXT,
        keterangan TEXT, status TEXT DEFAULT 'menunggu', dibuat TEXT);""")
    return con


def q(sql, p=()):
    con = _con()
    try:
        return [dict(r) for r in con.execute(sql, p).fetchall()]
    finally:
        con.close()


def x(sql, p=()):
    con = _con()
    try:
        cur = con.execute(sql, p)
        con.commit()
        return cur.lastrowid, cur.rowcount
    finally:
        con.close()


def sekarang():
    return datetime.now().isoformat(timespec="minutes")


def kontak_valid(k):
    k = (k or "").strip()
    return bool(re.fullmatch(r"\+?[0-9][0-9 \-]{7,15}", k) or re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", k))


def kata(*teks):
    return {w for w in re.findall(r"\w+", " ".join(t or "" for t in teks).lower()) if len(w) > 2}


def publik(b):
    return {k: v for k, v in b.items() if k not in ("kontak", "pin", "foto")}


# ---------- Logika inti ----------
def cocok(b, batas=3):
    lawan = "ditemukan" if b["status"] == "hilang" else "hilang"
    kb = kata(b["nama"], b["deskripsi"], b["lokasi"])
    hasil = []
    for c in q("SELECT * FROM barang WHERE selesai=0 AND status=? AND id!=?", (lawan, b["id"])):
        skor = len(kb & kata(c["nama"], c["deskripsi"], c["lokasi"])) + (2 if c["kategori"] == b["kategori"] else 0)
        if skor >= 3:
            hasil.append((skor, c))
    hasil.sort(key=lambda t: -t[0])
    return [c for _, c in hasil[:batas]]


def simpan(status, nama, kategori, deskripsi, lokasi, kontak, tanggal=""):
    status = status.lower().strip()
    if status not in ("hilang", "ditemukan"):
        return {"error": "status harus 'hilang' atau 'ditemukan'"}
    if not nama.strip() or not lokasi.strip():
        return {"error": "nama barang dan lokasi wajib diisi"}
    if not kontak_valid(kontak):
        return {"error": "kontak harus berupa nomor WhatsApp atau email yang valid"}
    kategori = kategori if kategori in KATEGORI else "Lainnya"
    pin = f"{secrets.randbelow(10000):04d}"
    i, _ = x("INSERT INTO barang(status,nama,kategori,deskripsi,lokasi,tanggal,kontak,pin,dibuat) VALUES(?,?,?,?,?,?,?,?,?)",
             (status, nama.strip(), kategori, deskripsi.strip(), lokasi.strip(),
              tanggal or date.today().isoformat(), kontak.strip(), pin, sekarang()))
    b = q("SELECT * FROM barang WHERE id=?", (i,))[0]
    return {"id": i, "pin": pin, "kandidat_cocok": [publik(c) for c in cocok(b)]}


def cari(kata_kunci="", status="", kategori="", selesai=False, batas=20):
    sql, p = "SELECT * FROM barang WHERE 1=1", []
    if not selesai:
        sql += " AND selesai=0"
    if status:
        sql += " AND status=?"
        p.append(status)
    if kategori and kategori != "Semua":
        sql += " AND kategori=?"
        p.append(kategori)
    kk = kata(kata_kunci)
    if kk:
        sql += " AND (" + " OR ".join(["(lower(nama||' '||deskripsi||' '||lokasi||' '||kategori) LIKE ?)"] * len(kk)) + ")"
        p += [f"%{w}%" for w in kk]
    return q(sql + " ORDER BY id DESC LIMIT ?", p + [batas])


# ---------- Tools untuk AI ----------
def lapor_barang(status: str, nama: str, kategori: str, deskripsi: str, lokasi: str, kontak: str) -> dict:
    """Simpan laporan barang hilang atau ditemukan.

    Args:
        status: 'hilang' jika pemilik kehilangan, 'ditemukan' jika ada yang menemukan.
        nama: Nama barang, misalnya 'Dompet'.
        kategori: Salah satu kategori barang.
        deskripsi: Ciri-ciri barang (warna, merek, stiker, isi).
        lokasi: Tempat barang hilang atau ditemukan.
        kontak: Nomor WhatsApp atau email pelapor.
    """
    return simpan(status, nama, kategori, deskripsi, lokasi, kontak)


def cari_barang(kata_kunci: str = "", status: str = "", kategori: str = "") -> list:
    """Cari barang yang belum selesai. Kata kunci kosong menampilkan laporan terbaru.

    Args:
        kata_kunci: Nama, warna, merek, atau lokasi barang.
        status: Opsional, 'hilang' atau 'ditemukan'.
        kategori: Opsional, kategori barang.
    """
    return [publik(b) for b in cari(kata_kunci, status.lower().strip(), kategori, batas=10)]


def ajukan_tanggapan(id_barang: int, nama: str, kontak: str, keterangan: str) -> dict:
    """Ajukan klaim kepemilikan atau info penemuan untuk sebuah laporan. Akan diverifikasi admin.

    Args:
        id_barang: ID laporan yang dituju.
        nama: Nama pengaju.
        kontak: Nomor WhatsApp atau email pengaju.
        keterangan: Ciri khusus barang (untuk klaim) atau tempat dan kondisi barang (untuk penemuan).
    """
    if not kontak_valid(kontak) or not keterangan.strip():
        return {"error": "kontak valid dan keterangan wajib diisi"}
    if not q("SELECT id FROM barang WHERE id=? AND selesai=0", (int(id_barang),)):
        return {"error": "laporan tidak ditemukan atau sudah selesai"}
    i, _ = x("INSERT INTO tanggapan(barang_id,nama,kontak,keterangan,dibuat) VALUES(?,?,?,?,?)",
             (int(id_barang), nama.strip(), kontak.strip(), keterangan.strip(), sekarang()))
    return {"berhasil": True, "id_tanggapan": i, "info": "menunggu verifikasi admin"}


def cek_tanggapan(kontak: str) -> list:
    """Cek status klaim atau tanggapan berdasarkan kontak yang dipakai saat mengajukan.

    Args:
        kontak: Nomor WhatsApp atau email yang dipakai saat mengajukan.
    """
    hasil = []
    for t in q("SELECT * FROM tanggapan WHERE kontak=? ORDER BY id DESC", (kontak.strip(),)):
        b = q("SELECT * FROM barang WHERE id=?", (t["barang_id"],))
        item = {"id_barang": t["barang_id"], "barang": b[0]["nama"] if b else "(dihapus)", "status": t["status"]}
        if t["status"] == "disetujui" and b:
            item["kontak_pelapor"] = b[0]["kontak"]
        hasil.append(item)
    return hasil


def tutup_laporan(id_barang: int, pin: str) -> dict:
    """Tutup laporan milik sendiri (barang sudah ketemu atau dikembalikan).

    Args:
        id_barang: ID laporan.
        pin: PIN 4 digit yang diberikan saat melapor.
    """
    _, n = x("UPDATE barang SET selesai=1, catatan='ditutup pelapor' WHERE id=? AND pin=? AND selesai=0",
             (int(id_barang), str(pin).strip()))
    return {"berhasil": n > 0, "pesan": "laporan ditutup" if n else "ID atau PIN salah, atau laporan sudah selesai"}


def statistik() -> dict:
    """Tampilkan ringkasan jumlah laporan."""
    def n(w):
        return q(f"SELECT COUNT(*) c FROM barang WHERE {w}")[0]["c"]
    return {"hilang_aktif": n("status='hilang' AND selesai=0"), "ditemukan_aktif": n("status='ditemukan' AND selesai=0"),
            "selesai": n("selesai=1"), "total": n("1=1")}


TOOLS = {f.__name__: f for f in (lapor_barang, cari_barang, ajukan_tanggapan, cek_tanggapan, tutup_laporan, statistik)}


def jalankan_agent(messages, model):
    for _ in range(6):
        resp = ollama.chat(model=model, messages=messages, tools=list(TOOLS.values()),
                           options={"temperature": 0.2})
        messages.append(resp.message)
        if not resp.message.tool_calls:
            return resp.message.content or "Maaf, bisa diulang dengan kalimat lain?"
        for call in resp.message.tool_calls:
            fn = TOOLS.get(call.function.name)
            try:
                hasil = fn(**call.function.arguments) if fn else {"error": "tool tidak dikenal"}
            except Exception as e:
                hasil = {"error": str(e)}
            messages.append({"role": "tool", "tool_name": call.function.name, "content": str(hasil)})
    return "Maaf, saya belum bisa menyelesaikan permintaan ini. Coba ulangi dengan kalimat lain."


# ---------- Tampilan ----------
def kartu(b, admin=False):
    with st.container(border=True):
        c1, c2 = st.columns([1, 4])
        if b.get("foto") and Path(b["foto"]).exists():
            c1.image(b["foto"], use_container_width=True)
        else:
            c1.caption("🖼️ Tanpa foto")

        label = "🔴 Hilang" if b["status"] == "hilang" else "🟢 Ditemukan"
        c2.markdown(f"### {b['nama']} { '✅' if b['selesai'] else ''}")
        c2.markdown(f"**{label}** | {b['kategori']} | {b['lokasi']}")
        c2.caption(f"📅 {b['tanggal']}")
        c2.info(b["deskripsi"] or "Tidak ada deskripsi")
        if admin:
            c2.warning(f"👤 Kontak: {b['kontak']} | 🔑 PIN: {b['pin']}")


def tab_chat():
    if "ui" not in st.session_state:
        st.session_state.ui = []
        st.session_state.msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    for r, t in st.session_state.ui:
        st.chat_message(r).write(t)
    if teks := st.chat_input("Contoh: saya kehilangan dompet hitam di kantin"):
        st.chat_message("user").write(teks)
        st.session_state.ui.append(("user", teks))
        st.session_state.msgs.append({"role": "user", "content": teks})
        with st.chat_message("assistant"):
            with st.spinner("Berpikir..."):
                try:
                    jawab = jalankan_agent(st.session_state.msgs, st.session_state.model)
                except Exception as e:
                    jawab = f"Maaf, ada kendala: {e}. Pastikan Ollama aktif dan model '{st.session_state.model}' sudah di-pull."
            st.write(jawab)
        st.session_state.ui.append(("assistant", jawab))


def tab_lapor():
    with st.form("lapor", clear_on_submit=True):
        status = st.radio("Jenis laporan", ["hilang", "ditemukan"], horizontal=True,
                          format_func=lambda s: "Saya kehilangan barang" if s == "hilang" else "Saya menemukan barang")
        nama = st.text_input("Nama barang *")
        kategori = st.selectbox("Kategori", KATEGORI)
        deskripsi = st.text_area("Ciri-ciri (warna, merek, stiker, isi)")
        c1, c2 = st.columns(2)
        lok = c1.selectbox("Lokasi", LOKASI)
        detail = c2.text_input("Detail lokasi (lantai, ruang)")
        tgl = st.date_input("Tanggal kejadian", value=date.today(), max_value=date.today())
        kontak = st.text_input("Kontak (WhatsApp atau email) *")
        foto = st.file_uploader("Foto barang (opsional)", type=["png", "jpg", "jpeg"])
        kirim = st.form_submit_button("Kirim laporan")
    if not kirim:
        return
    if foto and foto.size > 5 * 1024 * 1024:
        st.error("Ukuran foto maksimal 5 MB.")
        return
    r = simpan(status, nama, kategori, deskripsi, f"{lok} {detail}".strip(), kontak, tgl.isoformat())
    if "error" in r:
        st.error(r["error"])
        return
    if foto:
        path = FOTO_DIR / f"{r['id']}{Path(foto.name).suffix.lower()}"
        path.write_bytes(foto.getbuffer())
        x("UPDATE barang SET foto=? WHERE id=?", (str(path), r["id"]))
    st.success(f"Laporan tersimpan. ID: **{r['id']}** · PIN: **{r['pin']}** (simpan PIN untuk menutup laporan).")
    if r["kandidat_cocok"]:
        st.info("Ada laporan yang mungkin cocok:")
        for c in r["kandidat_cocok"]:
            kartu(q("SELECT * FROM barang WHERE id=?", (c["id"],))[0])


def tab_cari():
    c1, c2, c3 = st.columns([3, 1, 1])
    kk = c1.text_input("Cari barang", placeholder="dompet hitam, tumbler biru, kunci motor...")
    st_ = c2.selectbox("Jenis", ["", "hilang", "ditemukan"], format_func=lambda s: s or "Semua")
    kat = c3.selectbox("Kategori", ["Semua"] + KATEGORI)
    hasil = cari(kk, st_, kat)
    st.caption(f"{len(hasil)} laporan aktif ditemukan. Kontak disembunyikan; gunakan tombol di bawah laporan.")
    for b in hasil:
        kartu(b)
        ditemukan = b["status"] == "ditemukan"
        with st.expander("Ini barang saya (ajukan klaim)" if ditemukan else "Saya menemukan barang ini"):
            with st.form(f"tg{b['id']}", clear_on_submit=True):
                nama = st.text_input("Nama Anda")
                kontak = st.text_input("Kontak Anda (WhatsApp/email)")
                ket = st.text_area("Sebutkan ciri khusus yang hanya pemilik tahu" if ditemukan
                                   else "Di mana dan bagaimana kondisi barang saat ditemukan?")
                if st.form_submit_button("Kirim"):
                    r = ajukan_tanggapan(b["id"], nama, kontak, ket)
                    st.success("Terkirim, menunggu verifikasi admin.") if r.get("berhasil") else st.error(r["error"])


def tab_status():
    kontak = st.text_input("Masukkan kontak yang Anda pakai saat mengajukan klaim")
    if kontak:
        data = cek_tanggapan(kontak)
        if not data:
            st.info("Belum ada pengajuan dengan kontak itu.")
        for d in data:
            ikon = {"menunggu": "⏳", "disetujui": "✅", "ditolak": "❌"}[d["status"]]
            st.write(f"{ikon} Laporan #{d['id_barang']} ({d['barang']}): **{d['status']}**")
            if "kontak_pelapor" in d:
                st.success(f"Silakan hubungi pelapor: {d['kontak_pelapor']}")
    st.divider()
    st.subheader("Tutup laporan saya")
    with st.form("tutup"):
        i = st.number_input("ID laporan", min_value=1, step=1)
        pin = st.text_input("PIN 4 digit", type="password")
        if st.form_submit_button("Tutup laporan"):
            r = tutup_laporan(int(i), pin)
            st.success(r["pesan"]) if r["berhasil"] else st.error(r["pesan"])


def tab_statistik():
    s = statistik()
    c = st.columns(4)
    for col, (k, v) in zip(c, [("Hilang aktif", s["hilang_aktif"]), ("Ditemukan aktif", s["ditemukan_aktif"]),
                               ("Selesai", s["selesai"]), ("Total", s["total"])]):
        col.metric(k, v)
    rows = q("SELECT kategori, COUNT(*) n FROM barang GROUP BY kategori")
    if rows:
        st.bar_chart(pd.DataFrame(rows).set_index("kategori"))


def tab_admin():
    if not st.session_state.get("admin"):
        pw = st.text_input("Password admin", type="password")
        if st.button("Masuk") and secrets.compare_digest(pw.encode(), ADMIN_PASSWORD.encode()):
            st.session_state.admin = True
            st.rerun()
        if ADMIN_PASSWORD == "admin123":
            st.caption("⚠️ Password masih default. Ganti lewat variabel lingkungan ADMIN_PASSWORD.")
        return
    st.subheader("Antrian verifikasi")
    antrian = q("SELECT * FROM tanggapan WHERE status='menunggu' ORDER BY id")
    if not antrian:
        st.caption("Tidak ada tanggapan yang menunggu.")
    for t in antrian:
        b = q("SELECT * FROM barang WHERE id=?", (t["barang_id"],))
        if not b:
            continue
        with st.container(border=True):
            st.markdown(f"**Tanggapan #{t['id']}** untuk laporan #{b[0]['id']} ({b[0]['status']}: {b[0]['nama']})")
            st.write(f"{t['nama']} · {t['kontak']}: {t['keterangan']}")
            st.caption(f"Pelapor: {b[0]['kontak']}")
            c1, c2 = st.columns(2)
            if c1.button("✅ Setujui", key=f"ok{t['id']}"):
                x("UPDATE tanggapan SET status='disetujui' WHERE id=?", (t["id"],))
                st.rerun()
            if c2.button("❌ Tolak", key=f"no{t['id']}"):
                x("UPDATE tanggapan SET status='ditolak' WHERE id=?", (t["id"],))
                st.rerun()
    st.divider()
    st.subheader("Semua laporan")
    filt = st.radio("Tampilkan", ["aktif", "selesai", "semua"], horizontal=True)
    where = {"aktif": "selesai=0", "selesai": "selesai=1", "semua": "1=1"}[filt]
    for b in q(f"SELECT * FROM barang WHERE {where} ORDER BY id DESC LIMIT 50"):
        kartu(b, admin=True)
        c1, c2, _ = st.columns([1, 1, 4])
        if not b["selesai"] and c1.button("Selesai", key=f"s{b['id']}"):
            x("UPDATE barang SET selesai=1, catatan='dikembalikan (admin)' WHERE id=?", (b["id"],))
            st.rerun()
        if c2.button("Hapus", key=f"h{b['id']}"):
            x("DELETE FROM tanggapan WHERE barang_id=?", (b["id"],))
            x("DELETE FROM barang WHERE id=?", (b["id"],))
            st.rerun()
    st.divider()
    hari = st.number_input("Arsipkan laporan aktif yang lebih lama dari (hari)", 7, 365, 60)
    if st.button("Arsipkan laporan lama"):
        batas = (date.today() - timedelta(days=int(hari))).isoformat()
        _, n = x("UPDATE barang SET selesai=1, catatan='kedaluwarsa' WHERE selesai=0 AND tanggal < ?", (batas,))
        st.success(f"{n} laporan diarsipkan.")
    df = pd.DataFrame(q("SELECT * FROM barang")).drop(columns=["pin"], errors="ignore")
    st.download_button("Unduh semua laporan (CSV)", df.to_csv(index=False).encode("utf-8"), "laporan_lostfound.csv")


def main():
    st.set_page_config(page_title="Lost & Found Unpri", page_icon="🎒", layout="wide")

    # --- MODERN UI INJECTION ---
    st.markdown("""
        <style>
        .stApp { background-color: #f8f9fa; }
        .stButton>button { border-radius: 10px; font-weight: bold; transition: 0.3s; }
        .stButton>button:hover { transform: scale(1.05); border-color: #ff4b4b; color: #ff4b4b; }
        div[data-testid="stExpander"] { border: none !important; box-shadow: 0 2px 10px rgba(0,0,0,0.05); border-radius: 15px !important; background: white; }
        .stMetric { background: white; padding: 15px; border-radius: 15px; box-shadow: 0 2px 10px rgba(0,0,0,0.05); }
        h1, h2, h3 { color: #1e3a8a; font-family: 'Inter', sans-serif; }
        </style>
    """, unsafe_allow_html=True)

    st.title("🎒 AI Asisten Lost and Found Unpri")
    with st.sidebar:
        st.header("Pengaturan")
        st.session_state.model = st.text_input("Model Ollama", st.session_state.get("model", "llama3.2"))
        try:
            ollama.list()
            st.success("Ollama aktif")
        except Exception:
            st.error("Ollama belum aktif")
        st.caption("Laporan dan data tersimpan di unpri_lostfound.db")
    tabs = st.tabs(["💬 Asisten AI", "📝 Lapor", "🔎 Cari", "📬 Status & Tutup", "📊 Statistik", "🛡️ Admin"])
    for tab, fn in zip(tabs, [tab_chat, tab_lapor, tab_cari, tab_status, tab_statistik, tab_admin]):
        with tab:
            fn()


main()
