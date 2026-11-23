# -*- coding: utf-8 -*-
"""
Battery Cell Viewer - UVP / OVP 23 Cells
Membaca file ageing battery pack (.CDS), memfilter kondisi UVP (SOC 0%) dan
OVP (SOC 100%), lalu menampilkan diagram batang tegangan 23 cell.
Target: Windows, portable (dibuild menjadi satu file .exe dengan PyInstaller).
"""
import json
import math
import os
import re
import shutil
import sqlite3
import struct
import sys
import tempfile
import threading
import urllib.request
from collections import Counter
from datetime import datetime, timedelta
from statistics import mean, median

N_CELLS = 23
APP_TITLE = "Battery Ageing - UVP / OVP Cell Viewer (23 Cells)"
# Tema futuristik (gelap + neon)
BG, PANEL, PANEL2, BORDER = "#0a0f1e", "#111a30", "#17223d", "#26365e"
FG, MUTED, ACC = "#d8e6ff", "#7f92b8", "#00e5ff"
COL_UVP = "#ff3b5c"   # MERAH  -> UVP / SOC 0%
COL_OVP = "#2bff88"   # HIJAU  -> OVP / SOC 100%


# =====================================================================
#  CORE (tanpa GUI) - parsing file dan filter UVP/OVP
# =====================================================================
def base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(name):
    """Lokasi file bawaan, baik saat dijalankan sebagai .py maupun sebagai .exe (PyInstaller)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def split_line(line, d):
    if d == " ":
        return line.split()
    return [f.strip().strip('"') for f in line.split(d)]


def num(s, dec_comma=False):
    s = s.strip()
    if dec_comma:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return float("nan")


def numfrac(fields, dc):
    if not fields:
        return 0.0
    return sum(1 for f in fields if not math.isnan(num(f, dc))) / len(fields)


def detect_delim(sample):
    best, best_score = None, 0.0
    for d in [",", ";", "\t", "|", " "]:
        counts = [len(split_line(l, d)) for l in sample]
        mode, freq = Counter(counts).most_common(1)[0]
        score = (mode - 1) * freq / len(counts)
        if mode >= 3 and score > best_score:
            best, best_score = d, score
    if best is None:
        raise ValueError("Pemisah kolom (delimiter) tidak dikenali. "
                         "Pastikan file .CDS berbentuk teks/tabel.")
    return best


def parse_cds(path):
    """Baca file .CDS berbasis teks (CSV/TSV/semicolon/spasi) -> dict."""
    raw = open(path, "rb").read()
    has_bom16 = raw.startswith((b"\xff\xfe", b"\xfe\xff"))
    if not has_bom16 and raw[:4096].count(b"\x00") > 50:
        raise ValueError("File tampak berformat biner (bukan teks), "
                         "sehingga belum bisa dibaca oleh aplikasi ini.")
    if has_bom16:
        text = raw.decode("utf-16")
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")

    lines = [(n + 1, l) for n, l in enumerate(text.splitlines()) if l.strip()]
    if len(lines) < 3:
        raise ValueError("File kosong atau terlalu pendek.")

    delim = detect_delim([l for _, l in lines[:200]])
    dc = delim != ","
    sp = [split_line(l, delim) for _, l in lines]

    start = None
    for i in range(len(sp) - 1):
        a, b = sp[i], sp[i + 1]
        if len(a) >= 3 and len(a) == len(b) and numfrac(a, dc) >= 0.6 and numfrac(b, dc) >= 0.6:
            start = i
            break
    if start is None:
        raise ValueError("Baris data numerik tidak ditemukan di dalam file.")

    ncols = len(sp[start])
    headers = None
    for j in range(start - 1, -1, -1):
        if len(sp[j]) == ncols and numfrac(sp[j], dc) < 0.5:
            headers = sp[j]
            break
    if headers is None:
        headers = ["Kolom %d" % (i + 1) for i in range(ncols)]

    rows, linenos = [], []
    for i in range(start, len(sp)):
        if len(sp[i]) == ncols:
            rows.append(sp[i])
            linenos.append(lines[i][0])
    return {"headers": headers, "rows": rows, "linenos": linenos,
            "delim": delim, "dc": dc}


CELL_PATTERNS = [
    re.compile(r"(?:cell|cel|sel)\D{0,16}?0*(\d{1,2})(?!\d)", re.I),
    re.compile(r"^(?:c|v|cv|u|vc)[\s_\-]?0*(\d{1,2})(?:\s*[\(\[].*[\)\]])?$", re.I),
]
EXCLUDE = ("temp", "ntc", "bal", "soc", "soh", "res", "imp")


def auto_detect(headers):
    found = {}
    for i, h in enumerate(headers):
        hl = h.lower()
        if any(x in hl for x in EXCLUDE):
            continue
        for p in CELL_PATTERNS:
            m = p.search(h.strip())
            if m:
                n = int(m.group(1))
                if 1 <= n <= N_CELLS and n not in found:
                    found[n] = i
                break
    soc_idx = next((i for i, h in enumerate(headers)
                    if "soc" in h.lower() and "soh" not in h.lower()), None)
    time_idx = next((i for i, h in enumerate(headers)
                     if any(k in h.lower() for k in ("time", "date", "waktu"))), None)
    cell_idx = [found[n] for n in sorted(found)]
    complete = len(found) == N_CELLS
    return cell_idx, complete, soc_idx, time_idx


def build_dataset(parsed, cell_idx, soc_idx, time_idx):
    dc = parsed["dc"]
    raw = [[num(r[c], dc) for c in cell_idx] for r in parsed["rows"]]
    allv = [v for row in raw for v in row if not math.isnan(v)]
    if not allv:
        raise ValueError("Kolom cell yang dipilih tidak berisi angka.")
    scale = 0.001 if median(allv) > 100 else 1.0   # mV -> V
    cells = []
    for row in raw:
        cells.append(None if any(math.isnan(v) for v in row) else [v * scale for v in row])

    soc = None
    if soc_idx is not None:
        soc = [num(r[soc_idx], dc) for r in parsed["rows"]]
        valid = [s for s in soc if not math.isnan(s)]
        if valid and max(valid) <= 1.5:
            soc = [s * 100 for s in soc]
    times = [r[time_idx] for r in parsed["rows"]] if time_idx is not None else None
    cyc_idx = next((i for i, h in enumerate(parsed["headers"])
                    if any(k in h.lower() for k in ("cycle", "siklus"))), None)
    cycle = None
    if cyc_idx is not None:
        cycle = [int(c) if not math.isnan(c) else 0
                 for c in (num(r[cyc_idx], dc) for r in parsed["rows"])]
    return {"cells": cells, "soc": soc, "times": times, "cycle": cycle,
            "linenos": parsed["linenos"], "unit_mv": scale != 1.0}


SQLITE_MAGIC = b"SQLite format 3\x00"


def is_sqlite(path):
    with open(path, "rb") as f:
        return f.read(16) == SQLITE_MAGIC


def _filetime_str(ft):
    try:
        return (datetime(1601, 1, 1) + timedelta(microseconds=ft // 10)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "-"


def read_cds_sqlite(path):
    """Baca .CDS native (database SQLite dari tester baterai).
    Tata letak kolom dibaca dari tabel RecordSampDec_*; data dari RecordData_<step>.
    Setiap record = blockSize byte (344), nilai float32 little-endian."""
    tmp = None
    try:
        uri = __import__("pathlib").Path(path).resolve().as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True)
        con.execute("select count(*) from sqlite_master").fetchone()
    except sqlite3.Error:
        tmp = os.path.join(tempfile.gettempdir(), "cds_copy_%d.db" % os.getpid())
        shutil.copyfile(path, tmp)
        con = sqlite3.connect(tmp)
    try:
        tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
        dec = [t for t in tables if t.startswith("RecordSampDec_")]
        if not dec:
            raise ValueError("Struktur kolom data (RecordSampDec_*) tidak ditemukan di file .CDS ini.")
        fields, block = {}, None
        for off, bsz, name, lang, dt, dl, prec in con.execute(
                'select offAddrInBlockData,blockSize,showName,languageResoureStr,'
                'dataType,dataLen,decimalPrecision from "%s"' % dec[0]):
            block = bsz
            fields[name] = (off, dt, dl, prec or 0)
            if lang:
                fields[lang] = (off, dt, dl, prec or 0)
        vc = [fields.get("VCELL_%d" % i) for i in range(1, N_CELLS + 1)]
        if any(v is None for v in vc):
            raise ValueError("Kolom VCELL_1..VCELL_%d tidak lengkap di file ini (ditemukan %d)."
                             % (N_CELLS, sum(v is not None for v in vc)))
        offs = [v[0] for v in vc]
        prec = vc[0][3]
        contiguous = offs == [offs[0] + 4 * i for i in range(N_CELLS)]
        o_soc = fields["SOC"][0] if "SOC" in fields else None
        o_cur = fields["STRING_CURRENT"][0] if "STRING_CURRENT" in fields else None
        o_vol = fields["STRING_VOLT"][0] if "STRING_VOLT" in fields else None
        o_time = fields["STRING_SYSTEM_TIME"][0] if "STRING_SYSTEM_TIME" in fields else None
        o_cyc = fields["CYCLE"][0] if "CYCLE" in fields else None

        recs = sorted((int(m.group(1)), t) for t in tables
                      for m in [re.fullmatch(r"RecordData_(\d+)", t)] if m)
        if not recs:
            raise ValueError("Tabel RecordData_* tidak ditemukan.")
        cells, soc, times, steps, cur, vol, cyc_raw = [], [], [], [], [], [], []
        for step, t in recs:
            for blob, cnt in con.execute('select RecordDataBlock,Count from "%s" order by DataID' % t):
                for k in range(len(blob) // block):
                    b = k * block
                    if contiguous:
                        vals = list(struct.unpack_from("<%df" % N_CELLS, blob, b + offs[0]))
                    else:
                        vals = [struct.unpack_from("<f", blob, b + o)[0] for o in offs]
                    if any(math.isnan(v) or math.isinf(v) for v in vals):
                        cells.append(None)
                    else:
                        cells.append([round(v, prec) for v in vals])
                    soc.append(struct.unpack_from("<f", blob, b + o_soc)[0] if o_soc is not None else float("nan"))
                    cur.append(struct.unpack_from("<f", blob, b + o_cur)[0] if o_cur is not None else float("nan"))
                    vol.append(struct.unpack_from("<f", blob, b + o_vol)[0] if o_vol is not None else float("nan"))
                    times.append(_filetime_str(struct.unpack_from("<Q", blob, b + o_time)[0])
                                 if o_time is not None else "-")
                    steps.append(step)
                    cyc_raw.append(struct.unpack_from("<f", blob, b + o_cyc)[0] if o_cyc is not None else 0.0)
        if not cells:
            raise ValueError("Tidak ada record data di dalam file.")
        cycle = None
        if o_cyc is not None:
            vals = [0.0 if (math.isnan(v) or math.isinf(v)) else v for v in cyc_raw]
            if any(abs(v - round(v)) > 1e-3 for v in vals):   # tersimpan skala 0.001 (0.019 = cycle 19)
                vals = [v * 1000.0 for v in vals]
            cycle = [int(round(v)) for v in vals]
        info = {}
        try:
            r = con.execute("select ComputerName,TemplateName from ChannelStartInfo").fetchone()
            info = {"pc": r[0], "template": r[1]}
        except Exception:
            pass
        return {"cells": cells,
                "soc": soc if o_soc is not None else None,
                "times": times, "linenos": list(range(1, len(cells) + 1)),
                "steps": steps, "current": cur, "vpack": vol, "cycle": cycle,
                "unit_mv": False, "source": "sqlite", "info": info}
    finally:
        con.close()
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def _events(mask, keyf, pick_min, gap, pick, cyc):
    """Kembalikan dict {nomor_cycle: indeks_baris}. Run dipecah di pergantian cycle."""
    runs, cur, last = [], [], None
    for i, m in enumerate(mask):
        if not m:
            continue
        if cur and (i - last > gap + 1 or (cyc is not None and cyc[i] != cyc[cur[-1]])):
            runs.append(cur)
            cur = []
        cur.append(i)
        last = i
    if cur:
        runs.append(cur)

    def choose(rows):
        if pick == "first":
            return min(rows)
        if pick == "last":
            return max(rows)
        return (min if pick_min else max)(rows, key=keyf)

    picked = [choose(r) for r in runs]
    out = {}
    if cyc is None:
        for n, r in enumerate(picked):
            out[n + 1] = r
    else:
        groups = {}
        for r in picked:
            groups.setdefault(cyc[r], []).append(r)
        for k in sorted(groups):
            out[k] = choose(groups[k])
    return out


def analyze_core(ds, mode, tol, uvp_v, ovp_v, gap, pick="extreme"):
    """Return (uvp, ovp, mode_dipakai); uvp/ovp = {cycle: indeks baris}."""
    cells = ds["cells"]
    if mode == "soc" and ds["soc"] is not None:
        soc = ds["soc"]
        um = [c is not None and not math.isnan(s) and s <= tol for c, s in zip(cells, soc)]
        om = [c is not None and not math.isnan(s) and s >= 100 - tol for c, s in zip(cells, soc)]
        used = "soc"
    else:
        um = [c is not None and min(c) <= uvp_v for c in cells]
        om = [c is not None and max(c) >= ovp_v for c in cells]
        used = "volt"
    cyc = ds.get("cycle")
    uvp = _events(um, lambda i: min(cells[i]), True, gap, pick, cyc)
    ovp = _events(om, lambda i: max(cells[i]), False, gap, pick, cyc)
    return uvp, ovp, used


def summarize(v):
    lo, hi = min(v), max(v)
    return {"min_cell": v.index(lo) + 1, "min_v": lo,
            "max_cell": v.index(hi) + 1, "max_v": hi,
            "spread_mv": (hi - lo) * 1000.0, "mean_v": mean(v)}


# =====================================================================
#  GUI  (tema futuristik)
# =====================================================================
PICKS = {"Ekstrem (cell terendah/tertinggi)": "extreme",
         "Rekaman pertama saat kondisi tercapai": "first",
         "Rekaman terakhir saat kondisi tercapai": "last"}
ALL_LABEL = "Semua cycle (rata-rata)"


def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    import numpy as np
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

    CFG = os.path.join(base_dir(), "cds_viewer_config.json")
    FONT = "Segoe UI"

    def load_cfg():
        try:
            return json.load(open(CFG, encoding="utf-8"))
        except Exception:
            return {}

    def save_cfg(c):
        try:
            json.dump(c, open(CFG, "w", encoding="utf-8"), indent=2)
        except Exception:
            pass

    def setup_style(root):
        root.configure(bg=BG)
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=BG, foreground=FG, fieldbackground=PANEL2,
                     bordercolor=BORDER, lightcolor=PANEL, darkcolor=PANEL,
                     troughcolor=PANEL, font=(FONT, 10))
        st.configure("TFrame", background=BG)
        st.configure("TLabel", background=BG, foreground=FG)
        st.configure("Muted.TLabel", foreground=MUTED)
        st.configure("TLabelframe", background=BG, bordercolor=BORDER)
        st.configure("TLabelframe.Label", background=BG, foreground=ACC,
                     font=(FONT, 10, "bold"))
        st.configure("TButton", background=PANEL2, foreground=ACC, bordercolor=ACC,
                     focusthickness=0, padding=(12, 5), font=(FONT, 10, "bold"))
        st.map("TButton", background=[("active", ACC), ("pressed", "#00b8cc")],
               foreground=[("active", BG)])
        for w in ("TRadiobutton", "TCheckbutton"):
            st.configure(w, background=BG, foreground=FG, indicatorcolor=PANEL2)
            st.map(w, background=[("active", BG)], foreground=[("active", ACC)],
                   indicatorcolor=[("selected", ACC), ("active", PANEL2)])
        for w in ("TCombobox", "TSpinbox"):
            st.configure(w, fieldbackground=PANEL2, background=PANEL2, foreground=FG,
                         arrowcolor=ACC, bordercolor=BORDER, insertcolor=FG)
        st.map("TCombobox", fieldbackground=[("readonly", PANEL2)],
               foreground=[("readonly", FG)], selectbackground=[("readonly", PANEL2)],
               selectforeground=[("readonly", FG)])
        st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG,
                     rowheight=20, bordercolor=BORDER)
        st.configure("Treeview.Heading", background=PANEL2, foreground=ACC,
                     font=(FONT, 9, "bold"), relief="flat")
        st.map("Treeview", background=[("selected", BORDER)])
        root.option_add("*TCombobox*Listbox.background", PANEL2)
        root.option_add("*TCombobox*Listbox.foreground", FG)
        root.option_add("*TCombobox*Listbox.selectBackground", ACC)
        root.option_add("*TCombobox*Listbox.selectForeground", BG)

    class MapDialog:
        """Dialog pemetaan kolom bila deteksi otomatis tidak lengkap (file teks)."""
        def __init__(self, parent, headers, pre_cells, pre_soc):
            self.result = None
            self.top = tk.Toplevel(parent)
            self.top.title("Pemetaan Kolom")
            self.top.configure(bg=BG)
            self.top.transient(parent)
            self.top.grab_set()
            ttk.Label(self.top, wraplength=460, justify="left", text=(
                "Kolom cell tidak terdeteksi lengkap. Pilih tepat %d kolom "
                "tegangan cell (urutan mengikuti urutan kolom di file) dan "
                "kolom SOC." % N_CELLS)).pack(padx=10, pady=8, anchor="w")
            f = ttk.Frame(self.top)
            f.pack(fill="x", padx=10)
            ttk.Label(f, text="Kolom SOC:").pack(side="left")
            self.soc_var = tk.StringVar()
            opts = ["(tidak ada)"] + ["%d: %s" % (i, h) for i, h in enumerate(headers)]
            self.cb = ttk.Combobox(f, textvariable=self.soc_var, values=opts,
                                   state="readonly", width=40)
            self.cb.current(0 if pre_soc is None else pre_soc + 1)
            self.cb.pack(side="left", padx=6)
            lf = ttk.Frame(self.top)
            lf.pack(fill="both", expand=True, padx=10, pady=8)
            self.lb = tk.Listbox(lf, selectmode="multiple", exportselection=False,
                                 height=18, width=60, bg=PANEL2, fg=FG,
                                 selectbackground=ACC, selectforeground=BG,
                                 highlightthickness=0, relief="flat")
            sb = ttk.Scrollbar(lf, command=self.lb.yview)
            self.lb.config(yscrollcommand=sb.set)
            for i, h in enumerate(headers):
                self.lb.insert("end", "%d: %s" % (i, h))
            for i in pre_cells:
                self.lb.selection_set(i)
            self.lb.pack(side="left", fill="both", expand=True)
            sb.pack(side="left", fill="y")
            ttk.Button(self.top, text="OK", command=self.ok).pack(pady=8)

        def ok(self):
            sel = list(self.lb.curselection())
            if len(sel) != N_CELLS:
                messagebox.showerror("Pemetaan", "Anda memilih %d kolom. Harus tepat %d."
                                     % (len(sel), N_CELLS), parent=self.top)
                return
            s = self.cb.current()
            self.result = (sel, None if s <= 0 else s - 1)
            self.top.destroy()

        def show(self):
            self.top.wait_window()
            return self.result

    class App:
        def __init__(self, root):
            self.root = root
            root.title(APP_TITLE)
            root.geometry("1420x900")
            root.minsize(1180, 760)
            setup_style(root)
            self.cfg = load_cfg()
            self.ds = None
            self.file_name = ""
            self.delim = ""
            self.uvp_ev, self.ovp_ev = {}, {}      # {cycle: indeks baris}
            self.cycles = []
            self.ev_values = [ALL_LABEL]
            self.sel_cycle = None
            self.used_mode = "soc"
            self.lab_u, self.lab_o = "UVP", "OVP"
            self.mode_var = tk.StringVar(value="soc")
            self.tol_var = tk.DoubleVar(value=0.5)
            self.uvp_v = tk.DoubleVar(value=2.80)
            self.ovp_v = tk.DoubleVar(value=4.20)
            self.gap_var = tk.IntVar(value=5)
            self.pick_var = tk.StringVar(value="Ekstrem (cell terendah/tertinggi)")
            self.event_var = tk.StringVar()
            self.split_var = tk.BooleanVar(value=False)
            self.build_ui()

        # ---------------- UI ----------------
        def card(self, parent, title, color):
            f = tk.Frame(parent, bg=PANEL, highlightbackground=color, highlightthickness=1)
            tk.Label(f, text=title, bg=PANEL, fg=MUTED,
                     font=(FONT, 8, "bold")).pack(anchor="w", padx=10, pady=(6, 0))
            v = tk.Label(f, text="-", bg=PANEL, fg=color, font=(FONT, 15, "bold"))
            v.pack(anchor="w", padx=10, pady=(0, 6))
            return f, v

        def build_ui(self):
            # --- header ---
            hd = tk.Frame(self.root, bg=BG)
            hd.pack(fill="x", padx=12, pady=(10, 0))
            tk.Label(hd, text="\u25C6 BATTERY CELL ANALYZER", bg=BG, fg=ACC,
                     font=(FONT, 17, "bold")).pack(side="left")
            tk.Label(hd, text="   UVP  \u00B7  OVP  \u00B7  23 CELLS", bg=BG, fg=MUTED,
                     font=(FONT, 10)).pack(side="left", pady=(7, 0))
            tk.Frame(self.root, bg=ACC, height=2).pack(fill="x", padx=12, pady=(6, 6))

            bar = ttk.Frame(self.root)
            bar.pack(fill="x", padx=12)
            ttk.Button(bar, text="BUKA FILE .CDS", command=self.open_file).pack(side="left")
            ttk.Button(bar, text="EKSPOR CSV", command=self.export_csv).pack(side="left", padx=6)
            ttk.Button(bar, text="KIRIM KE API", command=self.api_dialog).pack(side="left")
            self.file_lbl = ttk.Label(bar, text="Belum ada file dimuat.", style="Muted.TLabel")
            self.file_lbl.pack(side="left", padx=14)

            # --- filter ---
            st = ttk.LabelFrame(self.root, text=" FILTER ", padding=8)
            st.pack(fill="x", padx=12, pady=(8, 0))
            ttk.Radiobutton(st, text="Berdasarkan SOC", variable=self.mode_var,
                            value="soc").grid(row=0, column=0, sticky="w")
            ttk.Label(st, text="Toleransi SOC (%):").grid(row=0, column=1, padx=(12, 2))
            ttk.Spinbox(st, from_=0, to=5, increment=0.1, width=5,
                        textvariable=self.tol_var).grid(row=0, column=2)
            ttk.Radiobutton(st, text="Berdasarkan Tegangan Cell", variable=self.mode_var,
                            value="volt").grid(row=0, column=3, padx=(22, 0))
            ttk.Label(st, text="UVP \u2264 (V):").grid(row=0, column=4, padx=(12, 2))
            ttk.Spinbox(st, from_=0, to=5, increment=0.05, width=6,
                        textvariable=self.uvp_v).grid(row=0, column=5)
            ttk.Label(st, text="OVP \u2265 (V):").grid(row=0, column=6, padx=(12, 2))
            ttk.Spinbox(st, from_=0, to=5, increment=0.05, width=6,
                        textvariable=self.ovp_v).grid(row=0, column=7)
            ttk.Label(st, text="Gabung jeda (baris):").grid(row=0, column=8, padx=(22, 2))
            ttk.Spinbox(st, from_=0, to=1000, width=5,
                        textvariable=self.gap_var).grid(row=0, column=9)
            ttk.Label(st, text="Titik acuan:").grid(row=1, column=0, sticky="w", pady=(8, 0))
            ttk.Combobox(st, textvariable=self.pick_var, state="readonly", width=34,
                         values=list(PICKS)).grid(row=1, column=1, columnspan=4,
                                                  sticky="w", pady=(8, 0))
            ttk.Button(st, text="TERAPKAN", command=self.analyze).grid(
                row=0, column=10, rowspan=2, padx=(18, 0))

            # --- cycle ---
            sel = ttk.Frame(self.root)
            sel.pack(fill="x", padx=12, pady=(8, 0))
            ttk.Label(sel, text="CYCLE:", foreground=ACC,
                      font=(FONT, 10, "bold")).pack(side="left")
            ttk.Button(sel, text="\u25C0", width=3,
                       command=lambda: self.step_cycle(-1)).pack(side="left", padx=(8, 2))
            self.ev_cb = ttk.Combobox(sel, textvariable=self.event_var, state="readonly", width=26)
            self.ev_cb.pack(side="left")
            self.ev_cb.bind("<<ComboboxSelected>>", lambda e: self.refresh())
            ttk.Button(sel, text="\u25B6", width=3,
                       command=lambda: self.step_cycle(1)).pack(side="left", padx=(2, 12))
            ttk.Checkbutton(sel, text="Dua panel (skala Y terpisah / zoom)",
                            variable=self.split_var, command=self.refresh).pack(side="left", padx=10)

            # --- KPI cards ---
            kp = tk.Frame(self.root, bg=BG)
            kp.pack(fill="x", padx=12, pady=(10, 0))
            self.k_cycle, self.v_cycle = self.card(kp, "CYCLE TERPILIH", ACC)
            self.k_umin, self.v_umin = self.card(kp, "UVP \u00B7 CELL TERENDAH", COL_UVP)
            self.k_uspr, self.v_uspr = self.card(kp, "UVP \u00B7 SPREAD", COL_UVP)
            self.k_omax, self.v_omax = self.card(kp, "OVP \u00B7 CELL TERTINGGI", COL_OVP)
            self.k_ospr, self.v_ospr = self.card(kp, "OVP \u00B7 SPREAD", COL_OVP)
            for i, c in enumerate((self.k_cycle, self.k_umin, self.k_uspr, self.k_omax, self.k_ospr)):
                kp.columnconfigure(i, weight=1, uniform="k")
                c.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 6, 0))

            # --- chart + tabel ---
            main = ttk.Frame(self.root)
            main.pack(fill="both", expand=True, padx=12, pady=10)
            left = tk.Frame(main, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
            left.pack(side="left", fill="both", expand=True)
            self.fig = Figure(figsize=(9, 5.5), dpi=100, facecolor=PANEL)
            self.canvas = FigureCanvasTkAgg(self.fig, master=left)
            self.canvas.get_tk_widget().configure(bg=PANEL, highlightthickness=0)
            self.canvas.get_tk_widget().pack(fill="both", expand=True)
            tb = NavigationToolbar2Tk(self.canvas, left)
            try:
                tb.config(background=PANEL)
                for w in tb.winfo_children():
                    try:
                        w.config(background=PANEL)
                    except tk.TclError:
                        pass
            except Exception:
                pass

            right = ttk.Frame(main, width=340)
            right.pack(side="right", fill="y", padx=(10, 0))
            cols = ("cell", "uvp", "ovp", "delta")
            self.tree = ttk.Treeview(right, columns=cols, show="headings", height=23)
            for c, t, w in (("cell", "CELL", 48), ("uvp", "UVP (V)", 88),
                            ("ovp", "OVP (V)", 88), ("delta", "\u0394 (V)", 88)):
                self.tree.heading(c, text=t)
                self.tree.column(c, width=w, anchor="center")
            self.tree.tag_configure("lo", foreground=COL_UVP)
            self.tree.tag_configure("hi", foreground=COL_OVP)
            self.tree.pack(fill="x")
            self.info_lbl = tk.Label(right, bg=BG, fg=MUTED, justify="left", anchor="w",
                                     font=("Consolas", 9), text="")
            self.info_lbl.pack(anchor="w", fill="x", pady=8)

            self.status = tk.Label(self.root, text="Siap.", bg=PANEL, fg=MUTED, anchor="w",
                                   font=(FONT, 9), padx=10, pady=3)
            self.status.pack(fill="x", side="bottom")
            self.draw_empty()

        # ---------------- file ----------------
        def open_file(self):
            p = filedialog.askopenfilename(
                title="Pilih file data ageing (.CDS)",
                filetypes=[("CDS data", "*.cds *.CDS"), ("Semua file", "*.*")])
            if not p:
                return
            try:
                if is_sqlite(p):
                    self.ds = read_cds_sqlite(p)
                    self.delim = "SQLite"
                else:
                    parsed = parse_cds(p)
                    cell_idx, complete, soc_idx, time_idx = auto_detect(parsed["headers"])
                    if not complete:
                        res = MapDialog(self.root, parsed["headers"], cell_idx, soc_idx).show()
                        if not res:
                            return
                        cell_idx, soc_idx = res
                    self.ds = build_dataset(parsed, cell_idx, soc_idx, time_idx)
                    self.delim = parsed["delim"]
            except Exception as e:
                messagebox.showerror("Gagal membaca file", str(e))
                return
            self.file_name = os.path.basename(p)
            self.file_lbl.config(text="%s   (%d rekaman)" % (self.file_name, len(self.ds["cells"])))
            self.mode_var.set("soc" if self.ds["soc"] is not None else "volt")
            self.analyze()

        def analyze(self):
            if not self.ds:
                return
            try:
                uvp, ovp, used = analyze_core(
                    self.ds, self.mode_var.get(), self.tol_var.get(),
                    self.uvp_v.get(), self.ovp_v.get(), self.gap_var.get(),
                    PICKS.get(self.pick_var.get(), "extreme"))
            except tk.TclError:
                messagebox.showerror("Pengaturan", "Nilai pengaturan tidak valid.")
                return
            self.used_mode = used
            if used == "soc":
                self.lab_u, self.lab_o = "UVP (SOC 0%)", "OVP (SOC 100%)"
            else:
                self.lab_u = "UVP (cell min \u2264 %.2f V)" % self.uvp_v.get()
                self.lab_o = "OVP (cell max \u2265 %.2f V)" % self.ovp_v.get()
            self.uvp_ev, self.ovp_ev = uvp, ovp
            self.cycles = sorted(set(uvp) | set(ovp))
            self.ev_values = [ALL_LABEL] + ["Cycle %d" % c for c in self.cycles]
            self.ev_cb["values"] = self.ev_values
            self.event_var.set(self.ev_values[1] if self.cycles else self.ev_values[0])
            has_cyc = bool(self.ds.get("cycle"))
            note = ""
            if self.mode_var.get() == "soc" and used == "volt":
                note = "  [SOC tidak ada di file -> memakai filter tegangan]"
            self.status.config(text="UVP: %d cycle | OVP: %d cycle | mode: %s | kolom Cycle: %s | "
                                    "sumber: %s%s" % (
                len(uvp), len(ovp), "SOC" if used == "soc" else "Tegangan",
                "dari file" if has_cyc else "tidak ada (urut event)", self.delim, note))
            if not uvp and not ovp:
                messagebox.showwarning(
                    "Tidak ada data", "Tidak ada baris UVP maupun OVP yang cocok.\n"
                    "Coba naikkan toleransi SOC atau gunakan filter tegangan.")
            self.refresh()

        def step_cycle(self, d):
            if not self.cycles:
                return
            vals = self.ev_values
            try:
                i = vals.index(self.event_var.get())
            except ValueError:
                i = 0
            i = max(0, min(len(vals) - 1, i + d))
            self.event_var.set(vals[i])
            self.refresh()

        # ---------------- data terpilih ----------------
        def ev_info(self, kind, row):
            ds = self.ds
            if row is None:
                return "%s: -" % kind
            t = ds["times"][row] if ds["times"] else "-"
            sc = "%.1f" % ds["soc"][row] if ds["soc"] and not math.isnan(ds["soc"][row]) else "-"
            extra = ""
            if ds.get("steps"):
                extra = ", step %d, I=%.2fA" % (ds["steps"][row], ds["current"][row])
            lab = "rekaman" if ds.get("source") == "sqlite" else "baris"
            return "%s: %s #%d%s\n     SOC %s | %s" % (kind, lab, ds["linenos"][row], extra, sc, t)

        def current(self):
            sel = self.event_var.get()
            cells = self.ds["cells"]

            def avg(rows):
                if not rows:
                    return None
                return [mean(cells[r][c] for r in rows) for c in range(N_CELLS)]
            if sel.startswith("Semua"):
                self.sel_cycle = None
                return (avg(list(self.uvp_ev.values())), avg(list(self.ovp_ev.values())), sel,
                        "Rata-rata dari %d cycle UVP dan %d cycle OVP" %
                        (len(self.uvp_ev), len(self.ovp_ev)))
            k = int(sel.split()[1])
            self.sel_cycle = k
            ur, orr = self.uvp_ev.get(k), self.ovp_ev.get(k)
            return (cells[ur] if ur is not None else None,
                    cells[orr] if orr is not None else None, sel,
                    self.ev_info("UVP", ur) + "\n" + self.ev_info("OVP", orr))

        # ---------------- tampilan ----------------
        def refresh(self):
            if not self.ds:
                return
            u, o, label, info = self.current()
            self.draw(u, o, label)
            su = summarize(u) if u else None
            so = summarize(o) if o else None
            self.tree.delete(*self.tree.get_children())
            for c in range(N_CELLS):
                a = "%.4f" % u[c] if u else "-"
                b = "%.4f" % o[c] if o else "-"
                d = "%.4f" % (o[c] - u[c]) if (u and o) else "-"
                tag = ()
                if su and su["min_cell"] == c + 1:
                    tag = ("lo",)
                elif so and so["max_cell"] == c + 1:
                    tag = ("hi",)
                self.tree.insert("", "end", values=(c + 1, a, b, d), tags=tag)
            self.v_cycle.config(text=label.replace("Semua cycle (rata-rata)", "RATA-RATA").upper())
            self.v_umin.config(text="C%d \u00B7 %.3f V" % (su["min_cell"], su["min_v"]) if su else "-")
            self.v_uspr.config(text="%.0f mV" % su["spread_mv"] if su else "-")
            self.v_omax.config(text="C%d \u00B7 %.3f V" % (so["max_cell"], so["max_v"]) if so else "-")
            self.v_ospr.config(text="%.0f mV" % so["spread_mv"] if so else "-")
            self.info_lbl.config(text=info)

        def style_ax(self, ax):
            ax.set_facecolor(PANEL)
            for sp in ax.spines.values():
                sp.set_color(BORDER)
            ax.tick_params(colors=FG, labelsize=9)
            ax.xaxis.label.set_color(FG)
            ax.yaxis.label.set_color(FG)
            ax.grid(axis="y", color=BORDER, alpha=0.6, linewidth=0.6)
            ax.set_axisbelow(True)

        def draw_empty(self):
            self.fig.clear()
            self.fig.patch.set_facecolor(PANEL)
            ax = self.fig.add_subplot(111)
            ax.set_facecolor(PANEL)
            ax.text(0.5, 0.5, "Klik  'BUKA FILE .CDS'  untuk memuat data ageing",
                    ha="center", va="center", fontsize=13, color=MUTED)
            ax.axis("off")
            self.canvas.draw_idle()

        def _panel(self, ax, x, vals, name, color):
            self.style_ax(ax)
            if not vals:
                ax.text(0.5, 0.5, "Tidak ada data %s" % name, ha="center", va="center",
                        transform=ax.transAxes, color=MUTED)
                return
            ax.bar(x, vals, 0.92, color=color, alpha=0.16, zorder=1)       # efek glow
            bars = ax.bar(x, vals, 0.68, color=color, alpha=0.95, zorder=2)
            lo, hi = min(vals), max(vals)
            rng = max(hi - lo, 0.005)
            bars[vals.index(hi)].set_edgecolor("white")
            bars[vals.index(hi)].set_linewidth(1.8)
            bars[vals.index(lo)].set_hatch("//")
            bars[vals.index(lo)].set_edgecolor("white")
            ax.axhline(mean(vals), ls="--", lw=1, color=ACC, alpha=0.8, zorder=3)
            ax.set_ylim(lo - rng * 0.6, hi + rng * 0.6)
            for xi, v in zip(x, vals):
                ax.text(xi, v, "%.3f" % v, ha="center", va="bottom", fontsize=7,
                        rotation=90, color=FG, zorder=4)
            ax.set_title("%s   \u00B7   spread %.0f mV   \u00B7   garis putus = rata-rata" % (name, rng * 1000),
                         fontsize=10, color=color, loc="left")
            ax.set_ylabel("Tegangan (V)")

        def draw(self, u, o, label):
            self.fig.clear()
            self.fig.patch.set_facecolor(PANEL)
            x = np.arange(1, N_CELLS + 1)
            if self.split_var.get():
                axes = self.fig.subplots(2, 1, sharex=True)
                self._panel(axes[0], x, u, self.lab_u, COL_UVP)
                self._panel(axes[1], x, o, self.lab_o, COL_OVP)
                axes[1].set_xlabel("Nomor Cell")
                axes[1].set_xticks(x)
            else:
                ax = self.fig.add_subplot(111)
                self.style_ax(ax)
                w = 0.4
                top = 0
                for vals, off, col, lab in ((u, -w / 2, COL_UVP, self.lab_u),
                                            (o, w / 2, COL_OVP, self.lab_o)):
                    if not vals:
                        continue
                    ax.bar(x + off, vals, w * 1.25, color=col, alpha=0.14, zorder=1)   # glow
                    ax.bar(x + off, vals, w * 0.92, color=col, alpha=0.95, zorder=2, label=lab)
                    top = max(top, max(vals))
                    for xi, v in zip(x, vals):
                        ax.text(xi + off, v, "%.3f" % v, ha="center", va="bottom",
                                fontsize=6, rotation=90, color=FG, zorder=4)
                ax.set_ylim(0, top * 1.15 if top else 1)
                ax.set_xticks(x)
                ax.set_xlabel("Nomor Cell")
                ax.set_ylabel("Tegangan (V)")
                leg = ax.legend(loc="upper left", ncol=2, facecolor=PANEL2, edgecolor=BORDER)
                for t in leg.get_texts():
                    t.set_color(FG)
            self.fig.suptitle("KONDISI %d CELL  \u00B7  UVP vs OVP  \u00B7  %s" % (N_CELLS, label.upper()),
                              fontsize=11, color=ACC, fontweight="bold")
            try:
                self.fig.tight_layout(rect=(0, 0, 1, 0.95))
            except Exception:
                pass
            self.canvas.draw_idle()

        # ---------------- ekspor ----------------
        def need_data(self):
            if not self.ds or not (self.uvp_ev or self.ovp_ev):
                messagebox.showinfo("Info", "Muat file dan pastikan ada data UVP/OVP dulu.")
                return False
            return True

        def export_csv(self):
            if not self.need_data():
                return
            p = filedialog.asksaveasfilename(
                defaultextension=".csv", filetypes=[("CSV", "*.csv")],
                initialfile=os.path.splitext(self.file_name)[0] + "_UVP_OVP.csv")
            if not p:
                return
            cells = self.ds["cells"]
            with open(p, "w", encoding="utf-8-sig", newline="") as f:
                f.write("Cycle,Cell,UVP_V,OVP_V,Delta_V\n")
                for cy in self.cycles:
                    ur, orr = self.uvp_ev.get(cy), self.ovp_ev.get(cy)
                    u = cells[ur] if ur is not None else None
                    o = cells[orr] if orr is not None else None
                    for c in range(N_CELLS):
                        a = "%.4f" % u[c] if u else ""
                        b = "%.4f" % o[c] if o else ""
                        d = "%.4f" % (o[c] - u[c]) if (u and o) else ""
                        f.write("%d,%d,%s,%s,%s\n" % (cy, c + 1, a, b, d))
            messagebox.showinfo("Ekspor", "Semua cycle tersimpan:\n" + p)

        def build_payload(self):
            u, o, label, _ = self.current()
            return {
                "source_file": self.file_name,
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "selection": label,
                "cycle": self.sel_cycle,
                "unit": "V",
                "cells": [{"cell": c + 1,
                           "uvp": round(u[c], 4) if u else None,
                           "ovp": round(o[c], 4) if o else None}
                          for c in range(N_CELLS)],
                "uvp_summary": summarize(u) if u else None,
                "ovp_summary": summarize(o) if o else None,
            }

        def api_dialog(self):
            if not self.need_data():
                return
            top = tk.Toplevel(self.root)
            top.title("Kirim ke API")
            top.configure(bg=BG)
            top.transient(self.root)
            top.grab_set()
            ttk.Label(top, text="URL endpoint (HTTP POST, JSON):").pack(anchor="w", padx=12, pady=(12, 0))
            url = tk.StringVar(value=self.cfg.get("api_url", ""))
            ttk.Entry(top, textvariable=url, width=60).pack(padx=12)
            ttk.Label(top, text="Bearer token (opsional, tidak disimpan):").pack(anchor="w", padx=12, pady=(8, 0))
            tok = tk.StringVar()
            ttk.Entry(top, textvariable=tok, width=60, show="*").pack(padx=12)

            def go():
                if not url.get().lower().startswith(("http://", "https://")):
                    messagebox.showerror("API", "URL harus diawali http:// atau https://", parent=top)
                    return
                self.cfg["api_url"] = url.get().strip()
                save_cfg(self.cfg)
                self.send_api(url.get().strip(), tok.get().strip())
                top.destroy()
            ttk.Button(top, text="KIRIM", command=go).pack(pady=12)

        def send_api(self, url, token):
            payload = self.build_payload()

            def work():
                try:
                    hdr = {"Content-Type": "application/json"}
                    if token:
                        hdr["Authorization"] = "Bearer " + token
                    req = urllib.request.Request(
                        url, data=json.dumps(payload).encode("utf-8"),
                        headers=hdr, method="POST")
                    with urllib.request.urlopen(req, timeout=20) as r:
                        msg = "Berhasil dikirim. HTTP %s" % r.status
                    self.root.after(0, lambda: messagebox.showinfo("API", msg))
                except Exception as e:
                    err = str(e)
                    self.root.after(0, lambda: messagebox.showerror("API gagal", err))
            threading.Thread(target=work, daemon=True).start()
            self.status.config(text="Mengirim data ke API...")

    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    try:  # agar ikon tampil benar di taskbar Windows
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("battery.cell.viewer")
    except Exception:
        pass
    root = tk.Tk()
    try:
        root.iconbitmap(resource_path("icon.ico"))
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    run_gui()
