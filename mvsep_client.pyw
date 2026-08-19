# MVSep Client - custom MVSep API GUI for Matteo Barni Photography
# tkinter + drag&drop + ffmpeg post-processing (fold mono / sample rate)
# API: https://mvsep.com/en/full_api
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import uuid
import urllib.parse
import urllib.request
from tkinter import filedialog, ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except Exception:
    HAS_DND = False

APP_NAME = "MVSep Client"
MIRRORS = {
    "main": "https://mvsep.com/api",
    "de": "https://de.mvsep.com/api",
    "de2": "https://de2.mvsep.com/api",
    "hk": "https://hk.mvsep.com/api",
}
ALGORITHMS_URL = "https://mvsep.com/api/app/algorithms?scopes=single_upload"
OUTPUT_FORMATS = {
    0: "MP3 320kbps",
    1: "WAV 16-bit",
    2: "FLAC 16-bit lossless",
    3: "M4A lossy",
    4: "WAV 32-bit float",
    5: "FLAC 24-bit lossless",
}
SAMPLE_RATES = ["keep", "44100", "48000", "88200", "96000", "176400", "192000"]
POLL_INTERVAL = 5
FFMPEG_FALLBACK = r"C:\Program Files\FFmpeg\bin\ffmpeg.exe"
LOCAL_MODELS_DIR = r"C:\Program Files\UVR5-UI\models"
LOCAL_SHIM = os.path.join(os.path.expanduser("~"), ".local", "bin", "audio-separator.exe")
SINGLE_STEMS = ["All stems", "Vocals", "Drums", "Bass", "Guitar", "Piano", "Other", "Instrumental"]
LOCAL_FORMATS = ["FLAC", "WAV", "MP3"]
DEFAULT_OUTDIR = os.path.join(os.path.expanduser("~"), "Downloads")
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console popup from windowed exe

# --- Dark theme (Aug 2026) ---
BG = "#1e1e1e"             # window background
FRAME_BG = "#252526"       # LabelFrame / inner frames
INPUT_BG = "#333333"       # entries, comboboxes
INPUT_BORDER = "#3a3a3a"
FG = "#e8e8e8"             # primary text
FG_MUTED = "#9d9d9d"
ACCENT = "#00b4d8"         # teal from the mvsep icon
ACCENT_HOVER = "#00cfe8"
ACCENT_DARK = "#007e99"
OK_COLOR = "#2ecc71"
ERR_COLOR = "#ff6b6b"
INFO_COLOR = FG
LIST_BG = "#1e1e1e"
SELECT_BG = "#007e99"

# Stem names MVSep / audio-separator can emit (lowercase, underscore-joined).
# smart_stem_name() matches the LONGEST known suffix so multi-word stems
# (lead_vocals, no_vocals) win over single-token ones.
KNOWN_STEMS = [
    "vocals", "drums", "bass", "guitar", "piano", "other", "others",
    "instrumental", "no_vocals", "lead_vocals", "backing_vocals",
    "lead", "backing", "voice", "keyboard", "keys", "synth", "strings",
    "organ", "acoustic_guitar", "electric_guitar", "acapella", "acappella",
    "accompaniment", "percussion", "wind", "brass",
]
# Stems considered 'secondary' — dropped when the discard toggle is on.
DISCARD_STEMS = {"other", "others"}


def resource_path(name):
    """Find a bundled/packaged resource (PyInstaller aware)."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        p = os.path.join(base, name)
        if os.path.exists(p):
            return p
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
    return p


def config_path():
    d = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "MVSepClient")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "config.json")


def load_config():
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    with open(config_path(), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def auto_import_token(cfg):
    """First-run: pull the token from the installed mvsep-cli config so the
    client works out of the box. Never logs/prints the token."""
    if cfg.get("token"):
        return cfg
    try:
        cli = os.path.join(os.path.expanduser("~"), ".mvsep_cli_config")
        with open(cli, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("api_token"):
            cfg["token"] = str(data["api_token"])
    except Exception:
        pass
    return cfg


def load_algorithms():
    """Load the model list: bundled resource -> app dir -> AppData cache."""
    candidates = [
        resource_path("algorithms.json"),
        os.path.join(os.environ.get("APPDATA", ""), "MVSepClient", "algorithms.json"),
    ]
    for p in candidates:
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list) and data:
                return data
        except Exception:
            continue
    return []


def cache_algorithms(data):
    try:
        p = os.path.join(os.environ.get("APPDATA", ""), "MVSepClient", "algorithms.json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


class MVSepAPI:
    def __init__(self, token, mirror="main"):
        self.token = token
        self.base = MIRRORS.get(mirror, MIRRORS["main"])

    def _request(self, url, data=None, headers=None, timeout=120):
        req = urllib.request.Request(url, data=data, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def fetch_algorithms(self):
        return self._request(ALGORITHMS_URL, timeout=60)

    def fetch_user(self):
        return self._request(
            f"{self.base}/app/user?api_token={urllib.parse.quote(self.token)}",
            timeout=30,
        )

    def create_separation(self, audiofile, sep_type, add_opts=None, output_format=2):
        boundary = "----mvsepclient" + uuid.uuid4().hex
        fields = [("api_token", self.token), ("sep_type", str(sep_type)),
                  ("output_format", str(output_format))]
        if add_opts:
            for k, v in add_opts.items():
                if v not in (None, ""):
                    fields.append((k, str(v)))
        body, ctype = multipart_encode(fields, audiofile, boundary)
        headers = {"Content-Type": ctype, "Content-Length": str(len(body))}
        return self._request(f"{self.base}/separation/create", data=body, headers=headers, timeout=300)

    def get_separation(self, job_hash):
        return self._request(
            f"{self.base}/separation/get?hash={urllib.parse.quote(job_hash)}&api_token={urllib.parse.quote(self.token)}",
            timeout=60,
        )

    def download(self, url, dest):
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=300) as resp, open(dest, "wb") as f:
            shutil.copyfileobj(resp, f)
        return dest


def multipart_encode(fields, filepath, boundary):
    """fields: list of (name, value). filepath: binary file for 'audiofile'."""
    import io
    buf = io.BytesIO()
    for name, value in fields:
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        buf.write(value.encode() + b"\r\n")
    fname = os.path.basename(filepath)
    buf.write(f"--{boundary}\r\n".encode())
    buf.write(f'Content-Disposition: form-data; name="audiofile"; filename="{fname}"\r\n'.encode())
    buf.write(b"Content-Type: application/octet-stream\r\n\r\n")
    with open(filepath, "rb") as f:
        buf.write(f.read())
    buf.write(f"\r\n--{boundary}--\r\n".encode())
    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


def find_ffmpeg():
    exe_dir = os.path.dirname(sys.executable)
    bundled = os.path.join(exe_dir, "ffmpeg.exe")
    if os.path.exists(bundled):
        return bundled
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    return FFMPEG_FALLBACK if os.path.exists(FFMPEG_FALLBACK) else "ffmpeg"


def parse_stem_files(files):
    """Normalize data.files from /separation/get into [(url, filename)]."""
    out = []
    if not isinstance(files, list):
        return out
    for item in files:
        if isinstance(item, str):
            out.append((item, os.path.basename(urllib.parse.urlparse(item).path) or "output"))
        elif isinstance(item, dict):
            url = item.get("url") or item.get("link") or item.get("file")
            name = item.get("download") or item.get("filename") or item.get("name") or os.path.basename(urllib.parse.urlparse(str(url)).path) or "output"
            if url:
                out.append((str(url), str(name)))
    return out


def smart_stem_name(filename):
    """Extract a clean stem name from an engine output filename.

    'somebody-to-love_piano-model_mt_5_piano.flac' -> 'piano'
    'song_model_lead_vocals.wav'                    -> 'lead vocals'
    'test_tone_(vocals)_bs_roformer_vocals_v3e.flac' -> 'vocals'  (local engine)
    Falls back to the last underscore token when no known stem matches.
    """
    base = os.path.splitext(os.path.basename(filename))[0].strip()
    if "_" not in base and "(" not in base:
        return base.lower() or "stem"
    # audio-separator (local engine) embeds the stem in parens:
    # test_tone_(vocals)_bs_roformer_vocals_v3e.flac
    m = re.search(r"\(([^)]+)\)", base)
    if m:
        cand = m.group(1).strip().lower().replace(" ", "_")
        if cand in KNOWN_STEMS:
            return cand.replace("_", " ")
    parts = base.split("_")
    lower = [p.strip().lower() for p in parts]
    for n in range(len(parts), 0, -1):
        cand = "_".join(lower[-n:])
        if cand in KNOWN_STEMS:
            return cand.replace("_", " ")
    return lower[-1] or "stem"


def unique_path(out_dir, name):
    """Return a path in out_dir that doesn't collide (name, name_1, name_2…)."""
    base, ext = os.path.splitext(name)
    cand = os.path.join(out_dir, name)
    n = 1
    while os.path.exists(cand):
        cand = os.path.join(out_dir, f"{base}_{n}{ext}")
        n += 1
    return cand


def ffmpeg_postprocess(src, out_dir, sample_rate, fold_mono, output_format=2, clean_base=None):
    """Resample + fold-to-mono a downloaded stem with ffmpeg (Matt's settings).

    clean_base: when given, the output is named <clean_base><ext> (no sr/mono
    suffixes) — Matt's 'intelligent rename' at the end of the pipeline."""
    ext = os.path.splitext(src)[1].lower()
    if ext == ".wav":
        codec = "pcm_f32le" if output_format == 4 else "pcm_s16le"
    elif ext == ".flac":
        codec = "flac"
    elif ext == ".mp3":
        codec = "libmp3lame"
    elif ext == ".m4a":
        codec = "aac"
    else:
        codec = "copy"
    out_name = os.path.basename(src)
    if clean_base:
        out_name = f"{clean_base}{ext}"
    elif sample_rate != "keep" and fold_mono:
        out_name = f"{os.path.splitext(out_name)[0]}_{sample_rate}_mono{ext}"
    elif sample_rate != "keep":
        out_name = f"{os.path.splitext(out_name)[0]}_{sample_rate}{ext}"
    elif fold_mono:
        out_name = f"{os.path.splitext(out_name)[0]}_mono{ext}"
    dest = unique_path(out_dir, out_name)
    af = []
    if sample_rate != "keep":
        af.append(f"aresample={sample_rate}:resampler=soxr:precision=28:dither_method=lipshitz")
    if fold_mono:
        af.append("pan=mono|c0=0.5*FL+0.5*FR")
    cmd = [find_ffmpeg(), "-y", "-i", src, "-map", "0:a:0"]
    if af:
        cmd += ["-af", ",".join(af)]
    cmd += ["-c:a", codec, dest]
    subprocess.run(cmd, check=True, capture_output=True, creationflags=CREATE_NO_WINDOW)
    return dest


class App:
    def __init__(self, root):
        self.root = root
        root.title("MVSep Client — Matteo Barni Photography")
        root.geometry("1100x860")
        root.minsize(900, 720)
        self._setup_dark_style()

        self.cfg = auto_import_token(load_config())
        self.algorithms = load_algorithms()
        self.files = []
        self.worker = None
        self.cancel_flag = threading.Event()
        self.msg_queue = queue.Queue()

        self._build_ui()
        if HAS_DND:
            self._enable_dnd()
        root.after(120, self._drain_queue)
        if not self.algorithms:
            # Cache empty on launch: auto-fetch the model list in the background
            # so the dropdown isn't stuck on "(no model list — click Refresh)".
            self.status_var.set("Model list empty — fetching…")
            self.refresh_models()
        self._center_window()

    # ---------- UI ----------
    def _setup_dark_style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=FG,
                        fieldbackground=INPUT_BG, bordercolor=INPUT_BORDER,
                        troughcolor=BG, arrowcolor=FG)
        style.configure("TFrame", background=FRAME_BG)
        style.configure("TLabel", background=FRAME_BG, foreground=FG)
        style.configure("TLabelframe", background=FRAME_BG, bordercolor=INPUT_BORDER,
                        lightcolor=INPUT_BORDER, darkcolor=INPUT_BORDER)
        style.configure("TLabelframe.Label", background=FRAME_BG, foreground=ACCENT,
                        font=("Segoe UI", 10, "bold"))
        style.configure("TEntry", fieldbackground=INPUT_BG, foreground=FG,
                        bordercolor=INPUT_BORDER, lightcolor=INPUT_BORDER,
                        darkcolor=INPUT_BORDER, insertcolor=FG)
        style.map("TEntry",
                  fieldbackground=[("focus", INPUT_BG)],
                  bordercolor=[("focus", ACCENT)],
                  lightcolor=[("focus", ACCENT)],
                  darkcolor=[("focus", ACCENT)])
        style.configure("TCombobox", fieldbackground=INPUT_BG, background=INPUT_BG,
                        foreground=FG, bordercolor=INPUT_BORDER,
                        lightcolor=INPUT_BORDER, darkcolor=INPUT_BORDER, arrowcolor=FG)
        style.map("TCombobox",
                  fieldbackground=[("readonly", INPUT_BG), ("focus", INPUT_BG)],
                  foreground=[("readonly", FG)],
                  bordercolor=[("focus", ACCENT)],
                  lightcolor=[("focus", ACCENT)],
                  darkcolor=[("focus", ACCENT)])
        self.root.option_add("*TCombobox*Listbox.background", INPUT_BG)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", SELECT_BG)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        self.root.option_add("*TCombobox*Listbox.borderWidth", 1)
        style.configure("TButton", background=INPUT_BG, foreground=FG,
                        bordercolor=INPUT_BORDER, lightcolor=INPUT_BORDER,
                        darkcolor=INPUT_BORDER, focuscolor=FRAME_BG, padding=(10, 4))
        style.map("TButton",
                  background=[("pressed", ACCENT_DARK), ("active", "#3d3d3d")],
                  foreground=[("disabled", FG_MUTED)])
        style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                        bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT,
                        focuscolor=ACCENT, font=("Segoe UI", 10, "bold"), padding=(14, 5))
        style.map("Accent.TButton",
                  background=[("pressed", ACCENT_DARK), ("active", ACCENT_HOVER)],
                  foreground=[("disabled", "#9fd6e4")])
        style.configure("TCheckbutton", background=FRAME_BG, foreground=FG,
                        indicatorcolor=INPUT_BG, bordercolor=INPUT_BORDER,
                        focuscolor=FRAME_BG)
        style.map("TCheckbutton",
                  background=[("active", FRAME_BG)],
                  indicatorcolor=[("selected", ACCENT)],
                  foreground=[("disabled", FG_MUTED)])
        style.configure("Big.Horizontal.TProgressbar", thickness=28,
                        background=ACCENT, troughcolor=BG, bordercolor=INPUT_BORDER,
                        lightcolor=ACCENT, darkcolor=ACCENT)
        self.root.configure(bg=BG)

    def _center_window(self):
        w, h = 1100, 860
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = max((sw - w) // 2, 0)
        y = max((sh - h) // 2, 0)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}
        root = self.root

        top = ttk.LabelFrame(root, text="API", padding=8)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="Token:").grid(row=0, column=0, sticky="w")
        self.token_var = tk.StringVar(value=self.cfg.get("token", ""))
        self.token_entry = ttk.Entry(top, textvariable=self.token_var, show="*", width=46)
        self.token_entry.grid(row=0, column=1, sticky="we", padx=4)
        self.show_token = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="show", variable=self.show_token,
                        command=self._toggle_token).grid(row=0, column=2)
        ttk.Button(top, text="Check", command=self.check_api).grid(row=0, column=3, padx=4)
        ttk.Label(top, text="Region:").grid(row=1, column=0, sticky="w")
        self.mirror_var = tk.StringVar(value=self.cfg.get("mirror", "main"))
        ttk.Combobox(top, textvariable=self.mirror_var, values=list(MIRRORS.keys()),
                     state="readonly", width=10).grid(row=1, column=1, sticky="w", padx=4, pady=(4, 0))
        ttk.Label(top, text="Output format:").grid(row=1, column=2, sticky="e")
        fmt_values = [f"{k} — {v}" for k, v in OUTPUT_FORMATS.items()]
        self.fmt_var = tk.StringVar(value=fmt_values[self.cfg.get("output_format", 2)])
        fmt_box = ttk.Combobox(top, textvariable=self.fmt_var, state="readonly", width=24)
        fmt_box["values"] = fmt_values
        fmt_box.grid(row=1, column=3, sticky="we", padx=4, pady=(4, 0))
        self.fmt_box = fmt_box
        top.columnconfigure(1, weight=1)

        # Model
        model = ttk.LabelFrame(root, text="Model", padding=8)
        model.pack(fill="x", **pad)
        top_row = ttk.Frame(model)
        top_row.pack(fill="x")
        ttk.Label(top_row, text="Source:").pack(side="left")
        self.source_var = tk.StringVar(value=self.cfg.get("source", "MVSep API"))
        self.source_box = ttk.Combobox(top_row, textvariable=self.source_var,
                                       values=["MVSep API", "Local (audio-separator)"],
                                       state="readonly", width=24)
        self.source_box.pack(side="left", padx=4)
        self.source_box.bind("<<ComboboxSelected>>", self._source_changed)

        # --- API engine body ---
        self.api_body = ttk.Frame(model)
        row = ttk.Frame(self.api_body)
        row.pack(fill="x")
        ttk.Label(row, text="Separation model:").pack(side="left")
        self.model_var = tk.StringVar()
        self.model_box = ttk.Combobox(row, textvariable=self.model_var, state="readonly", width=70)
        self.model_box.pack(side="left", padx=4, fill="x", expand=True)
        ttk.Button(row, text="Refresh list", command=self.refresh_models).pack(side="left")
        self.model_box.bind("<<ComboboxSelected>>", self._model_changed)

        self.opt_frame = ttk.Frame(self.api_body)
        self.opt_frame.pack(fill="x", pady=(6, 0))
        self.opt_widgets = []

        if self.algorithms:
            self._populate_models(self.algorithms)
        else:
            self.model_box["values"] = ["(no model list — click Refresh)"]
        if self.cfg.get("last_model") is not None:
            self._select_model_by_render(self.cfg["last_model"])

        # --- Local engine body ---
        self.local_body = ttk.Frame(model)
        lrow = ttk.Frame(self.local_body)
        lrow.pack(fill="x")
        ttk.Label(lrow, text="Model (.ckpt):").pack(side="left")
        self.local_model_var = tk.StringVar()
        self.local_model_box = ttk.Combobox(lrow, textvariable=self.local_model_var, state="readonly", width=62)
        self.local_model_box.pack(side="left", padx=4, fill="x", expand=True)
        ttk.Button(lrow, text="Rescan", command=self._load_local_models).pack(side="left")
        lrow2 = ttk.Frame(self.local_body)
        lrow2.pack(fill="x", pady=(6, 0))
        ttk.Label(lrow2, text="Stems:").pack(side="left")
        self.stem_var = tk.StringVar(value=self.cfg.get("local_stem", "All stems"))
        ttk.Combobox(lrow2, textvariable=self.stem_var, values=SINGLE_STEMS, state="readonly", width=18).pack(side="left", padx=4)
        ttk.Label(lrow2, text="Format:").pack(side="left", padx=(12, 0))
        self.local_fmt_var = tk.StringVar(value=self.cfg.get("local_format", "FLAC"))
        ttk.Combobox(lrow2, textvariable=self.local_fmt_var, values=LOCAL_FORMATS, state="readonly", width=10).pack(side="left", padx=4)
        self._load_local_models()

        self._apply_source()

        # Files
        files = ttk.LabelFrame(root, text="Files (drag & drop here)", padding=8)
        files.pack(fill="both", expand=True, **pad)
        self.file_list = tk.Listbox(files, height=7, selectmode="extended",
                                    bg=LIST_BG, fg=FG, selectbackground=SELECT_BG,
                                    selectforeground="#ffffff", relief="flat",
                                    highlightthickness=1, highlightbackground=INPUT_BORDER,
                                    highlightcolor=INPUT_BORDER, activestyle="none")
        self.file_list.pack(fill="both", expand=True, side="left", padx=(0, 4))
        fbtn = ttk.Frame(files)
        fbtn.pack(side="left", fill="y")
        ttk.Button(fbtn, text="Add files…", command=self.add_files).pack(fill="x", pady=2)
        ttk.Button(fbtn, text="Remove", command=self.remove_files).pack(fill="x", pady=2)
        ttk.Button(fbtn, text="Clear", command=self.clear_files).pack(fill="x", pady=2)

        # Output + post-processing
        out = ttk.LabelFrame(root, text="Output & conversion", padding=8)
        out.pack(fill="x", **pad)
        ttk.Label(out, text="Output folder:").grid(row=0, column=0, sticky="w")
        self.outdir_var = tk.StringVar(value=self.cfg.get("output_dir", DEFAULT_OUTDIR))
        ttk.Entry(out, textvariable=self.outdir_var).grid(row=0, column=1, sticky="we", padx=4)
        ttk.Button(out, text="Browse…", command=self.browse_outdir).grid(row=0, column=2)
        self.mono_var = tk.BooleanVar(value=self.cfg.get("fold_mono", False))
        ttk.Checkbutton(out, text="Fold to mono", variable=self.mono_var).grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Label(out, text="Sample rate:").grid(row=1, column=1, sticky="w", padx=4, pady=(4, 0))
        self.sr_var = tk.StringVar(value=self.cfg.get("sample_rate", "keep"))
        ttk.Combobox(out, textvariable=self.sr_var, values=SAMPLE_RATES, state="readonly", width=12).grid(row=1, column=1, sticky="w", padx=(64, 0), pady=(4, 0))
        self.discard_secondary_var = tk.BooleanVar(value=self.cfg.get("discard_secondary", False))
        ttk.Checkbutton(out, text="Discard secondary stem ('other')", variable=self.discard_secondary_var).grid(row=2, column=0, sticky="w", pady=(4, 0))
        out.columnconfigure(1, weight=1)

        # Run
        ctl = ttk.Frame(root)
        ctl.pack(fill="x", **pad)
        self.run_btn = ttk.Button(ctl, text="Start separation", command=self.start,
                                  style="Accent.TButton")
        self.run_btn.pack(side="left")
        self.cancel_btn = ttk.Button(ctl, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=6)

        # Status + loading bar
        statusf = ttk.LabelFrame(root, text="Status", padding=10)
        statusf.pack(fill="x", **pad)
        self.status_var = tk.StringVar(value="Ready.")
        self.status_lbl = ttk.Label(statusf, textvariable=self.status_var, wraplength=1040, font=("Segoe UI", 10))
        self.status_lbl.pack(fill="x")
        self.progress = ttk.Progressbar(statusf, mode="determinate", style="Big.Horizontal.TProgressbar")
        self.progress.pack(fill="x", pady=(8, 0))

    def _enable_dnd(self):
        self.file_list.drop_target_register(DND_FILES)
        self.file_list.dnd_bind("<<Drop>>", self._on_drop)

    def _on_drop(self, event):
        data = self.root.tk.splitlist(event.data)
        for p in data:
            if os.path.isfile(p):
                self._add_file(p)

    def _toggle_token(self):
        self.token_entry.config(show="" if self.show_token.get() else "*")

    def _populate_models(self, algorithms):
        self.algorithms = algorithms
        names = []
        for a in algorithms:
            label = a.get("name", "?")
            rid = a.get("render_id")
            names.append(f"{label}  [sep {rid}]" if rid is not None else label)
        self.model_box["values"] = names
        if names:
            self.model_box.current(0)
            self._model_changed()

    def _select_model_by_render(self, render_id):
        for i, a in enumerate(self.algorithms):
            if a.get("render_id") == render_id:
                self.model_box.current(i)
                self._model_changed()
                return

    def _source_changed(self, *_):
        self._apply_source()

    def _apply_source(self):
        if self.source_var.get() == "Local (audio-separator)":
            self.api_body.pack_forget()
            self.local_body.pack(fill="x")
        else:
            self.local_body.pack_forget()
            self.api_body.pack(fill="x")

    def _load_local_models(self):
        models = []
        if os.path.isdir(LOCAL_MODELS_DIR):
            models = sorted(
                f for f in os.listdir(LOCAL_MODELS_DIR)
                if f.lower().endswith((".ckpt", ".onnx"))
            )
        if models:
            self.local_model_box["values"] = models
            prev = self.cfg.get("local_model")
            if prev in models:
                self.local_model_var.set(prev)
            else:
                self.local_model_box.current(0)
        else:
            self.local_model_box["values"] = [f"(no models in {LOCAL_MODELS_DIR})"]
            self.local_model_box.current(0)

    def _selected_algo(self):
        idx = self.model_box.current()
        if 0 <= idx < len(self.algorithms):
            return self.algorithms[idx]
        return None

    def _model_changed(self, *_):
        for w in self.opt_widgets:
            w.destroy()
        self.opt_widgets.clear()
        algo = self._selected_algo()
        if not algo:
            return
        fields = algo.get("algorithm_fields") or []
        if not fields:
            ttk.Label(self.opt_frame, text="No additional options.").pack(side="left")
            return
        for fld in fields:
            name = fld.get("name", "")
            text = fld.get("text", name)
            opts = {}
            try:
                opts = json.loads(fld.get("options") or "{}")
            except Exception:
                opts = {}
            default_key = fld.get("default_key", "")
            box_frame = ttk.Frame(self.opt_frame)
            box_frame.pack(side="left", padx=(0, 10))
            ttk.Label(box_frame, text=f"{text}:").pack(side="left")
            var = tk.StringVar()
            combos = []
            for k, v in opts.items():
                combos.append(f"{k} — {v}")
            cb = ttk.Combobox(box_frame, textvariable=var, values=combos, state="readonly", width=42)
            cb.pack(side="left", padx=2)
            if combos:
                if default_key in opts:
                    cb.current(combos.index(f"{default_key} — {opts[default_key]}"))
                else:
                    cb.current(0)
            self.opt_widgets.extend([box_frame, cb])

    def _selected_add_opts(self):
        algo = self._selected_algo()
        if not algo:
            return {}
        opts = {}
        fields = algo.get("algorithm_fields") or []
        cbs = [w for w in self.opt_widgets if isinstance(w, ttk.Combobox)]
        for fld, cb in zip(fields, cbs):
            val = cb.get()
            if val:
                key = val.split(" — ", 1)[0].strip()
                opts[fld.get("name", "")] = key
        return opts

    def _log(self, text, tag="info"):
        self.msg_queue.put((text, tag))

    def _drain_queue(self):
        try:
            while True:
                text, tag = self.msg_queue.get_nowait()
                if tag == "progress":
                    self.progress.step(1)
                elif tag == "finish":
                    self._finish()
                elif tag == "models":
                    self._populate_models(text)
                    self.status_var.set(f"{len(text)} models loaded.")
                else:
                    self.status_var.set(text)
                    if tag == "err":
                        self.status_lbl.config(foreground=ERR_COLOR)
                    elif tag == "ok":
                        self.status_lbl.config(foreground=OK_COLOR)
                    else:
                        self.status_lbl.config(foreground=INFO_COLOR)
        except queue.Empty:
            pass
        self.root.after(120, self._drain_queue)

    # ---------- actions ----------
    def check_api(self):
        def work():
            self._log("Checking API…")
            try:
                api = MVSepAPI(self.token_var.get().strip(), self.mirror_var.get())
                user = api.fetch_user()
                data = user.get("data", user)
                plan = (data.get("plan") or {}).get("name") if isinstance(data.get("plan"), dict) else data.get("plan")
                self._log(f"API OK — user: {data.get('email', data.get('username', '?'))}, plan: {plan or data.get('is_premium')}", "ok")
            except Exception as e:
                self._log(f"API check failed: {e}", "err")
        threading.Thread(target=work, daemon=True).start()

    def refresh_models(self):
        def work():
            self._log("Fetching model list…")
            try:
                api = MVSepAPI(self.token_var.get().strip() or "none", self.mirror_var.get())
                data = api.fetch_algorithms()
                cache_algorithms(data)
                self._log(data, "models")
            except Exception as e:
                self._log(f"Model refresh failed: {e}", "err")
        threading.Thread(target=work, daemon=True).start()

    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="Select audio files",
            filetypes=[("Audio", "*.wav *.flac *.mp3 *.m4a *.aiff *.aif *.ogg *.opus *.wma *.m4b"), ("All files", "*.*")],
        )
        for p in paths:
            self._add_file(p)

    def _add_file(self, p):
        p = os.path.normpath(p)
        if p not in self.files:
            self.files.append(p)
            self.file_list.insert("end", p)

    def remove_files(self):
        sel = list(self.file_list.curselection())
        for i in reversed(sel):
            self.file_list.delete(i)
            del self.files[i]

    def clear_files(self):
        self.file_list.delete(0, "end")
        self.files = []

    def browse_outdir(self):
        d = filedialog.askdirectory(initialdir=self.outdir_var.get() or os.path.expanduser("~"))
        if d:
            self.outdir_var.set(d)

    def start(self):
        if self.worker and self.worker.is_alive():
            self._log("Already running.", "err")
            return
        token = self.token_var.get().strip()
        if not self.files:
            self._log("No files added.", "err")
            return
        source = self.source_var.get()
        algo = None
        if source == "Local (audio-separator)":
            if not self.local_model_var.get() or self.local_model_var.get().startswith("(no models"):
                self._log("No local model selected.", "err")
                return
            if not os.path.exists(LOCAL_SHIM):
                self._log(f"audio-separator not found at {LOCAL_SHIM}", "err")
                return
        else:
            if not token:
                self._log("API token missing.", "err")
                return
            algo = self._selected_algo()
            if not algo:
                self._log("No model selected.", "err")
                return
        outdir = self.outdir_var.get().strip() or os.getcwd()
        if not os.path.isdir(outdir):
            try:
                os.makedirs(outdir, exist_ok=True)
            except Exception as e:
                self._log(f"Cannot create output folder: {e}", "err")
                return
        try:
            sel_fmt = int(self.fmt_var.get().split(" — ")[0])
        except Exception:
            sel_fmt = 2
        self.cfg.update({
            "token": token,
            "mirror": self.mirror_var.get(),
            "output_format": sel_fmt,
            "output_dir": outdir,
            "fold_mono": self.mono_var.get(),
            "sample_rate": self.sr_var.get(),
            "discard_secondary": self.discard_secondary_var.get(),
            "source": source,
            "last_model": algo.get("render_id") if algo else self.cfg.get("last_model"),
            "local_model": self.local_model_var.get(),
            "local_stem": self.stem_var.get(),
            "local_format": self.local_fmt_var.get(),
        })
        save_config(self.cfg)
        self.cancel_flag.clear()
        self.run_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.progress.config(mode="indeterminate")
        self.progress.start(15)
        self._elapsed = 0
        self.root.after(1000, self._tick)
        params = {
            "source": source,
            "sep_type": algo.get("render_id") if algo else None,
            "add_opts": self._selected_add_opts() if algo else {},
            "output_format": sel_fmt,
            "fold_mono": self.mono_var.get(),
            "sample_rate": self.sr_var.get(),
            "discard_secondary": self.discard_secondary_var.get(),
            "mirror": self.mirror_var.get(),
            "token": token,
            "outdir": outdir,
            "local_model": self.local_model_var.get(),
            "local_stem": self.stem_var.get(),
            "local_format": self.local_fmt_var.get(),
        }
        self.worker = threading.Thread(target=self._dispatch, args=(list(self.files), params), daemon=True)
        self.worker.start()

    def cancel(self):
        self.cancel_flag.set()
        self._log("Cancel requested — finishing current step…", "info")

    def _finish(self, ok=True):
        self.run_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")
        try:
            self.progress.stop()
            self.progress.config(mode="determinate", value=100 if ok else 0)
        except Exception:
            pass

    def _tick(self):
        if self.worker and self.worker.is_alive():
            self._elapsed += 1
            self.status_lbl.config(text=f"{self.status_var.get()}  ·  {self._elapsed}s")
            self.root.after(1000, self._tick)
        else:
            self.status_lbl.config(text=self.status_var.get())

    def _dispatch(self, files, params):
        if params["source"] == "Local (audio-separator)":
            self._worker_local(files, params)
        else:
            self._worker(files, params)

    def _worker_local(self, files, params):
        outdir = params["outdir"]
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        done = 0
        for f in files:
            if self.cancel_flag.is_set():
                self._log("Cancelled.", "info")
                break
            self._log(f"=== {os.path.basename(f)} (local) ===")
            tmp = None
            try:
                tmp = tempfile.mkdtemp(prefix="mvsep_local_")
                cmd = [LOCAL_SHIM, "-m", params["local_model"],
                       "--model_file_dir", LOCAL_MODELS_DIR,
                       "--output_dir", tmp,
                       "--output_format", params["local_format"],
                       "--use_autocast"]
                if params["local_stem"] != "All stems":
                    cmd += ["--single_stem", params["local_stem"]]
                cmd += [f]
                self._log("Running audio-separator…")
                p = subprocess.run(cmd, env=env, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
                if p.returncode != 0:
                    self._log(f"audio-separator failed: {p.stderr[-500:]}", "err")
                    continue
                outputs = sorted(
                    os.path.join(tmp, x) for x in os.listdir(tmp)
                    if os.path.splitext(x)[1].lower() in (".flac", ".wav", ".mp3", ".m4a", ".ogg")
                    and x != os.path.basename(f)  # audio-separator copies the source into output_dir
                )
                if not outputs:
                    self._log("No output files produced.", "err")
                    continue
                if params.get("discard_secondary") and len(outputs) > 1:
                    outputs = [o for o in outputs if smart_stem_name(o) not in DISCARD_STEMS]
                for src in outputs:
                    if self.cancel_flag.is_set():
                        break
                    stem = smart_stem_name(src)
                    self._log(f"  {os.path.basename(src)} → converting…")
                    try:
                        final = ffmpeg_postprocess(src, outdir, params["sample_rate"], params["fold_mono"], None, clean_base=stem)
                        self._log(f"  ✓ {os.path.basename(final)}", "ok")
                    except Exception as e:
                        self._log(f"  ffmpeg failed ({e}); keeping raw output", "err")
                        shutil.copy(src, unique_path(outdir, f"{stem}{os.path.splitext(src)[1]}"))
            except Exception as e:
                self._log(f"ERROR: {e}", "err")
            finally:
                if tmp:
                    shutil.rmtree(tmp, ignore_errors=True)
            done += 1
            self._log("__done__", "progress")
        self._log("Finished.", "ok")
        self._log("__finish__", "finish")

    def _worker(self, files, params):
        api = MVSepAPI(params["token"], params["mirror"])
        outdir = params["outdir"]
        done = 0
        for f in files:
            if self.cancel_flag.is_set():
                self._log("Cancelled.", "info")
                break
            self._log(f"=== {os.path.basename(f)} ===")
            tmp = None
            try:
                tmp = tempfile.mkdtemp(prefix="mvsep_")
                self._log(f"Uploading {os.path.basename(f)}…")
                create = api.create_separation(
                    f,
                    params["sep_type"],
                    params["add_opts"],
                    params["output_format"],
                )
                if not create.get("success"):
                    raise RuntimeError(create.get("data", create))
                job_hash = create["data"].get("hash") or create["data"].get("id")
                self._log(f"Job created: {job_hash}")
                status = self._poll(api, job_hash)
                if status != "done":
                    self._log(f"Job failed/aborted: {status}", "err")
                    continue
                self._log("Downloading stems…")
                files_info = self._get_result_files(api, job_hash)
                if not files_info:
                    self._log("No output files returned.", "err")
                    continue
                for url, name in files_info:
                    if self.cancel_flag.is_set():
                        break
                    safe = "".join(c for c in name if c not in '\\/:*?"<>|')
                    stem = smart_stem_name(safe)
                    if params.get("discard_secondary") and len(files_info) > 1 and stem in DISCARD_STEMS:
                        self._log(f"  discarding secondary stem '{stem}'")
                        continue
                    dest = os.path.join(tmp, safe)
                    api.download(url, dest)
                    self._log(f"  {safe} → converting…")
                    try:
                        final = ffmpeg_postprocess(dest, outdir, params["sample_rate"], params["fold_mono"], params["output_format"], clean_base=stem)
                        self._log(f"  ✓ {os.path.basename(final)}", "ok")
                    except Exception as e:
                        self._log(f"  ffmpeg failed ({e}); keeping raw download", "err")
                        shutil.copy(dest, unique_path(outdir, f"{stem}{os.path.splitext(safe)[1]}"))
            except Exception as e:
                self._log(f"ERROR: {e}", "err")
            finally:
                if tmp:
                    shutil.rmtree(tmp, ignore_errors=True)
            done += 1
            self._log("__done__", "progress")
        self._log("Finished.", "ok")
        self._log("__finish__", "finish")

    def _get_result_files(self, api, job_hash):
        resp = api.get_separation(job_hash)
        return parse_stem_files((resp.get("data") or {}).get("files"))

    def _poll(self, api, job_hash):
        last = None
        errors = 0
        while not self.cancel_flag.is_set():
            try:
                resp = api.get_separation(job_hash)
            except Exception as e:
                errors += 1
                if errors > 5:
                    self._log(f"  poll failed {errors}x: {e}", "err")
                    return "error"
                time.sleep(POLL_INTERVAL)
                continue
            errors = 0
            status = resp.get("status", "")
            if status != last:
                self._log(f"  status: {status}")
                last = status
            if status == "done":
                return "done"
            if status == "failed":
                msg = (resp.get("data") or {}).get("message") or resp.get("message") or "unknown error"
                self._log(f"  failed: {msg}", "err")
                return "failed"
            time.sleep(POLL_INTERVAL)
        return "cancelled"


def main():
    if HAS_DND:
        root = TkinterDnD.Tk()
    else:
        root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
