#!/usr/bin/env python3
"""
Tesla Stitch - join Tesla Sentry/Dashcam camera angles into one video.

Detects each camera from the filename (e.g. 2026-10-05_14-22-10-left_pillar.mp4),
groups clips by timestamp, and joins them side by side with ffmpeg.

Needs: Python 3.8+ with tkinter (included in standard installers) and ffmpeg.
ffmpeg is looked up next to this program first, then on PATH.
"""
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
from collections import defaultdict
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "Tesla Stitch"
CAMERAS = ["left_pillar", "front", "right_pillar",
           "left_repeater", "right_repeater", "back"]
NONE = "(none)"
NAME_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})-(?P<cam>[a-z_]+)\.mp4$",
    re.IGNORECASE,
)
HW_ENCODERS = ["h264_nvenc", "h264_qsv", "h264_amf", "h264_videotoolbox"]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # hide console on Windows


# --------------------------------------------------------------------------- #
# core logic
# --------------------------------------------------------------------------- #
def find_ffmpeg():
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    spots = []
    if getattr(sys, "frozen", False):
        spots.append(Path(getattr(sys, "_MEIPASS", "")) / exe)  # bundled
        spots.append(Path(sys.executable).parent / exe)         # next to .exe
    else:
        spots.append(Path(__file__).resolve().parent / exe)
    for s in spots:
        if s.is_file():
            return str(s)
    return shutil.which("ffmpeg")


def available_encoders(ffmpeg):
    found = ["libx264"]
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=15,
                             creationflags=NO_WINDOW).stdout
        found += [e for e in HW_ENCODERS if e in out]
    except Exception:
        pass
    return found


def scan(folder: Path, recursive: bool):
    """{(parent_dir, timestamp): {camera: path}} from filenames."""
    groups = defaultdict(dict)
    it = folder.rglob("*.mp4") if recursive else folder.glob("*.mp4")
    for p in sorted(it):
        m = NAME_RE.match(p.name)
        if m:
            groups[(p.parent, m.group("ts"))][m.group("cam").lower()] = p
    return groups


def build_filter(n, height, overlap):
    """Scale to equal height, trim shared edges, hstack."""
    parts = []
    for i in range(n):
        lt = overlap if i > 0 else 0
        rt = overlap if i < n - 1 else 0
        f = f"[{i}:v]scale=-2:{height}"
        if lt or rt:
            f += f",crop=iw-{lt + rt}:ih:{lt}:0"
        parts.append(f + f"[v{i}]")
    parts.append("".join(f"[v{i}]" for i in range(n)) + f"hstack=inputs={n}[out]")
    return ";".join(parts)


def build_cmd(ffmpeg, files, layout, out, cfg):
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    for cam in layout:
        cmd += ["-i", str(files[cam])]
    cmd += ["-filter_complex", build_filter(len(layout), cfg["height"], cfg["overlap"]),
            "-map", "[out]", "-an", "-c:v", cfg["encoder"]]
    if cfg["encoder"] == "libx264":
        cmd += ["-preset", "veryfast", "-crf", str(cfg["crf"])]
    else:
        cmd += ["-b:v", "8M"]
    cmd += ["-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
    return cmd


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.minsize(660, 560)
        self.q = queue.Queue()
        self.proc = None
        self.stop = threading.Event()
        self.ffmpeg = find_ffmpeg()

        self.v_in = tk.StringVar()
        self.v_out = tk.StringVar()
        self.v_cams = [tk.StringVar(value="left_pillar"),
                       tk.StringVar(value="front"),
                       tk.StringVar(value="right_pillar")]
        self.v_overlap = tk.IntVar(value=80)
        self.v_height = tk.StringVar(value="960")
        self.v_enc = tk.StringVar(value="libx264")
        self.v_crf = tk.IntVar(value=23)
        self.v_recursive = tk.BooleanVar(value=True)
        self.v_force = tk.BooleanVar(value=False)

        self._build()
        if not self.ffmpeg:
            self.log("ffmpeg not found. Place ffmpeg next to this program "
                     "or install it and add it to PATH.")
            self.btn_start.state(["disabled"])
        else:
            self.cb_enc["values"] = available_encoders(self.ffmpeg)
        self.after(100, self._poll)

    # ---- layout -----------------------------------------------------------
    def _build(self):
        pad = {"padx": 8, "pady": 4}
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)
        root.columnconfigure(1, weight=1)

        ttk.Label(root, text="Clips folder").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(root, textvariable=self.v_in).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(root, text="Browse…", command=self._pick_in).grid(row=0, column=2, **pad)

        ttk.Label(root, text="Output folder").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(root, textvariable=self.v_out).grid(row=1, column=1, sticky="ew", **pad)
        ttk.Button(root, text="Browse…", command=self._pick_out).grid(row=1, column=2, **pad)

        # camera order
        lf = ttk.LabelFrame(root, text="Camera order (left → right)", padding=8)
        lf.grid(row=2, column=0, columnspan=3, sticky="ew", **pad)
        for i, var in enumerate(self.v_cams):
            vals = CAMERAS if i == 1 else [NONE] + CAMERAS
            ttk.Combobox(lf, textvariable=var, values=vals, state="readonly",
                         width=16).grid(row=0, column=i, padx=6)
            lf.columnconfigure(i, weight=1)

        # options
        of = ttk.LabelFrame(root, text="Options", padding=8)
        of.grid(row=3, column=0, columnspan=3, sticky="ew", **pad)
        ttk.Label(of, text="Overlap trim (px)").grid(row=0, column=0, sticky="w", padx=4, pady=3)
        ttk.Spinbox(of, from_=0, to=400, increment=10, width=7,
                    textvariable=self.v_overlap).grid(row=0, column=1, sticky="w")
        ttk.Label(of, text="Height (px)").grid(row=0, column=2, sticky="w", padx=(16, 4))
        ttk.Combobox(of, textvariable=self.v_height, width=7,
                     values=["480", "720", "960", "1080"]).grid(row=0, column=3, sticky="w")
        ttk.Label(of, text="Encoder").grid(row=1, column=0, sticky="w", padx=4, pady=3)
        self.cb_enc = ttk.Combobox(of, textvariable=self.v_enc, width=18,
                                   values=["libx264"], state="readonly")
        self.cb_enc.grid(row=1, column=1, sticky="w")
        ttk.Label(of, text="Quality (CRF)").grid(row=1, column=2, sticky="w", padx=(16, 4))
        ttk.Spinbox(of, from_=15, to=35, width=7,
                    textvariable=self.v_crf).grid(row=1, column=3, sticky="w")
        ttk.Checkbutton(of, text="Include subfolders",
                        variable=self.v_recursive).grid(row=2, column=0, columnspan=2, sticky="w", padx=4)
        ttk.Checkbutton(of, text="Overwrite existing",
                        variable=self.v_force).grid(row=2, column=2, columnspan=2, sticky="w", padx=(16, 4))
        ttk.Label(of, text="CRF applies to the software encoder; hardware encoders use 8 Mbps.",
                  foreground="gray").grid(row=3, column=0, columnspan=4, sticky="w", padx=4)

        # run controls
        bf = ttk.Frame(root)
        bf.grid(row=4, column=0, columnspan=3, sticky="ew", **pad)
        self.btn_start = ttk.Button(bf, text="Start", command=self._start)
        self.btn_start.pack(side="left")
        self.btn_cancel = ttk.Button(bf, text="Cancel", command=self._cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=6)
        self.btn_open = ttk.Button(bf, text="Open output folder", command=self._open_out)
        self.btn_open.pack(side="right")

        self.bar = ttk.Progressbar(root, mode="determinate")
        self.bar.grid(row=5, column=0, columnspan=3, sticky="ew", **pad)
        self.lbl = ttk.Label(root, text="Idle")
        self.lbl.grid(row=6, column=0, columnspan=3, sticky="w", padx=8)

        self.txt = tk.Text(root, height=10, state="disabled", wrap="word")
        self.txt.grid(row=7, column=0, columnspan=3, sticky="nsew", **pad)
        root.rowconfigure(7, weight=1)

    # ---- helpers ----------------------------------------------------------
    def log(self, s):
        self.txt.configure(state="normal")
        self.txt.insert("end", s + "\n")
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def _pick_in(self):
        d = filedialog.askdirectory(title="Select folder with Tesla clips")
        if d:
            self.v_in.set(d)
            if not self.v_out.get():
                self.v_out.set(str(Path(d) / "stitched"))

    def _pick_out(self):
        d = filedialog.askdirectory(title="Select output folder")
        if d:
            self.v_out.set(d)

    def _open_out(self):
        p = self.v_out.get()
        if not p or not Path(p).is_dir():
            return
        if sys.platform.startswith("win"):
            os.startfile(p)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])

    # ---- run --------------------------------------------------------------
    def _start(self):
        src = Path(self.v_in.get())
        if not src.is_dir():
            messagebox.showerror(APP_NAME, "Choose a valid clips folder.")
            return
        layout = [v.get() for v in self.v_cams if v.get() != NONE]
        if len(layout) < 2 or len(set(layout)) != len(layout):
            messagebox.showerror(APP_NAME, "Pick at least two different cameras.")
            return
        try:
            cfg = {"overlap": int(self.v_overlap.get()),
                   "height": int(self.v_height.get()),
                   "crf": int(self.v_crf.get()),
                   "encoder": self.v_enc.get(),
                   "force": self.v_force.get()}
        except (ValueError, tk.TclError):
            messagebox.showerror(APP_NAME, "Options must be whole numbers.")
            return
        out = Path(self.v_out.get() or src / "stitched")
        self.v_out.set(str(out))

        self.stop.clear()
        self.btn_start.state(["disabled"])
        self.btn_cancel.state(["!disabled"])
        self.bar["value"] = 0
        threading.Thread(target=self._work,
                         args=(src, out, layout, cfg, self.v_recursive.get()),
                         daemon=True).start()

    def _cancel(self):
        self.stop.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def _work(self, src, out_root, layout, cfg, recursive):
        q = self.q
        groups = scan(src, recursive)
        if not groups:
            q.put(("log", "No Tesla-named .mp4 files found."))
            q.put(("done", 0, 0, 0))
            return

        jobs, skipped = [], 0
        for (parent, ts), files in sorted(groups.items()):
            missing = [c for c in layout if c not in files]
            if missing:
                q.put(("log", f"skip {ts}: missing {', '.join(missing)}"))
                skipped += 1
                continue
            rel = parent.relative_to(src) if recursive else Path()
            jobs.append((ts, files, out_root / rel / f"{ts}-stitched.mp4"))

        total, done = len(jobs), 0
        q.put(("total", total))
        for i, (ts, files, out) in enumerate(jobs, 1):
            if self.stop.is_set():
                break
            if out.exists() and not cfg["force"]:
                q.put(("log", f"exists {out.name}"))
                q.put(("progress", i, total))
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            q.put(("status", f"Stitching {ts}  ({i}/{total})"))
            try:
                self.proc = subprocess.Popen(
                    build_cmd(self.ffmpeg, files, layout, out, cfg),
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    text=True, creationflags=NO_WINDOW)
                _, err = self.proc.communicate()
                rc = self.proc.returncode
            except Exception as e:
                rc, err = 1, str(e)
            if self.stop.is_set():
                out.unlink(missing_ok=True)  # drop partial file
                break
            if rc == 0:
                done += 1
                q.put(("log", f"ok    {out.name}"))
            else:
                skipped += 1
                out.unlink(missing_ok=True)
                q.put(("log", f"ERROR {ts}: {(err or '').strip()[-300:]}"))
            q.put(("progress", i, total))
        q.put(("done", done, skipped, total))

    def _poll(self):
        try:
            while True:
                m = self.q.get_nowait()
                if m[0] == "log":
                    self.log(m[1])
                elif m[0] == "status":
                    self.lbl.configure(text=m[1])
                elif m[0] == "total":
                    self.bar["maximum"] = max(m[1], 1)
                elif m[0] == "progress":
                    self.bar["value"] = m[1]
                elif m[0] == "done":
                    state = "Cancelled" if self.stop.is_set() else "Finished"
                    self.lbl.configure(text=f"{state}: {m[1]} stitched, {m[2]} skipped/failed")
                    self.log(f"{state}. {m[1]} stitched, {m[2]} skipped/failed.")
                    self.btn_start.state(["!disabled"])
                    self.btn_cancel.state(["disabled"])
        except queue.Empty:
            pass
        self.after(100, self._poll)


if __name__ == "__main__":
    App().mainloop()
