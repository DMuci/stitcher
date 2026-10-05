#!/usr/bin/env python3
"""
Tesla Stitch - join Tesla Sentry/Dashcam camera angles into one video.

Detects each camera from the filename (e.g. 2026-10-05_14-22-10-left_pillar.mp4),
groups clips by timestamp, and joins them side by side with ffmpeg. Optionally
joins the one-minute segments of an event into one video and trims it.

Needs: Python 3.8+ with tkinter (included in standard installers) and ffmpeg.
ffmpeg is looked up next to this program first, then on PATH.
"""
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from collections import defaultdict
from datetime import datetime
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
DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
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


def get_duration(ffmpeg, path):
    """Clip length in seconds (parsed from ffmpeg's header output)."""
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)],
                           capture_output=True, text=True, timeout=30,
                           creationflags=NO_WINDOW)
        m = DUR_RE.search(r.stderr)
        if m:
            return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    except Exception:
        pass
    return 60.0  # Tesla segments are ~1 minute


def parse_time(s):
    """'' -> None; '45', '1:30', '0:01:30.5' -> seconds. Raises ValueError."""
    s = s.strip()
    if not s:
        return None
    parts = s.split(":")
    if len(parts) > 3:
        raise ValueError(s)
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    if not math.isfinite(total) or total < 0:
        raise ValueError(s)
    return total


def fmt_time(sec):
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def ts_to_dt(ts):
    return datetime.strptime(ts, "%Y-%m-%d_%H-%M-%S")


def scan(folder: Path, recursive: bool):
    """{(parent_dir, timestamp): {camera: path}} from filenames."""
    groups = defaultdict(dict)
    it = folder.rglob("*.mp4") if recursive else folder.glob("*.mp4")
    for p in sorted(it):
        m = NAME_RE.match(p.name)
        if m:
            groups[(p.parent, m.group("ts"))][m.group("cam").lower()] = p
    return groups


def make_pieces(segs, start, stop):
    """
    segs: [(ts, files, duration)] in time order.
    Returns ([(seg_index, ss, length)], total_seconds). ss/length are None when
    the piece starts at / runs to the segment boundary.
    """
    total = sum(d for _, _, d in segs)
    lo_t = start or 0.0
    hi_t = min(stop, total) if stop is not None else total
    if lo_t >= hi_t:
        return [], total
    pieces, pos = [], 0.0
    for i, (_, _, d) in enumerate(segs):
        a, b = pos, pos + d
        lo, hi = max(lo_t, a), min(hi_t, b)
        if hi - lo > 0.05:
            ss = None if lo <= a + 1e-6 else lo - a
            ln = None if hi >= b - 1e-6 else hi - lo
            pieces.append((i, ss, ln))
        pos = b
    return pieces, total


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


def build_cmd(ffmpeg, files, layout, out, cfg, ss=None, length=None):
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    for cam in layout:
        if ss:
            cmd += ["-ss", f"{ss:.3f}"]
        if length:
            cmd += ["-t", f"{length:.3f}"]
        cmd += ["-i", str(files[cam])]
    cmd += ["-filter_complex", build_filter(len(layout), cfg["height"], cfg["overlap"]),
            "-map", "[out]", "-an", "-c:v", cfg["encoder"]]
    if cfg["encoder"] == "libx264":
        cmd += ["-preset", "veryfast", "-crf", str(cfg["crf"])]
    else:
        cmd += ["-b:v", "8M"]
    cmd += ["-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
    return cmd


def concat_cmd(ffmpeg, list_path, out):
    return [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(list_path),
            "-c", "copy", "-movflags", "+faststart", str(out)]


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.minsize(680, 640)
        self.q = queue.Queue()
        self.proc = None
        self.stop = threading.Event()
        self.ffmpeg = find_ffmpeg()
        self._n = 0
        self._total = 0

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
        self.v_join = tk.BooleanVar(value=True)
        self.v_start = tk.StringVar()
        self.v_stop = tk.StringVar()

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

        lf = ttk.LabelFrame(root, text="Camera order (left → right)", padding=8)
        lf.grid(row=2, column=0, columnspan=3, sticky="ew", **pad)
        for i, var in enumerate(self.v_cams):
            vals = CAMERAS if i == 1 else [NONE] + CAMERAS
            ttk.Combobox(lf, textvariable=var, values=vals, state="readonly",
                         width=16).grid(row=0, column=i, padx=6)
            lf.columnconfigure(i, weight=1)

        # trim / join
        tf = ttk.LabelFrame(root, text="Final video", padding=8)
        tf.grid(row=3, column=0, columnspan=3, sticky="ew", **pad)
        ttk.Checkbutton(tf, text="Join all clips of an event into one video",
                        variable=self.v_join).grid(row=0, column=0, columnspan=5, sticky="w", padx=4)
        ttk.Label(tf, text="Trim start").grid(row=1, column=0, sticky="w", padx=4, pady=3)
        ttk.Entry(tf, textvariable=self.v_start, width=10).grid(row=1, column=1, sticky="w")
        ttk.Label(tf, text="Trim stop").grid(row=1, column=2, sticky="w", padx=(16, 4))
        ttk.Entry(tf, textvariable=self.v_stop, width=10).grid(row=1, column=3, sticky="w")
        ttk.Label(tf, text="Format: seconds, m:ss or h:mm:ss. Blank = start/end of video. "
                           "Times count from the start of the joined video.",
                  foreground="gray", wraplength=600, justify="left"
                  ).grid(row=2, column=0, columnspan=5, sticky="w", padx=4)

        of = ttk.LabelFrame(root, text="Options", padding=8)
        of.grid(row=4, column=0, columnspan=3, sticky="ew", **pad)
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

        bf = ttk.Frame(root)
        bf.grid(row=5, column=0, columnspan=3, sticky="ew", **pad)
        self.btn_start = ttk.Button(bf, text="Start", command=self._start)
        self.btn_start.pack(side="left")
        self.btn_cancel = ttk.Button(bf, text="Cancel", command=self._cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=6)
        self.btn_open = ttk.Button(bf, text="Open output folder", command=self._open_out)
        self.btn_open.pack(side="right")

        self.bar = ttk.Progressbar(root, mode="determinate")
        self.bar.grid(row=6, column=0, columnspan=3, sticky="ew", **pad)
        self.lbl = ttk.Label(root, text="Idle")
        self.lbl.grid(row=7, column=0, columnspan=3, sticky="w", padx=8)

        self.txt = tk.Text(root, height=10, state="disabled", wrap="word")
        self.txt.grid(row=8, column=0, columnspan=3, sticky="nsew", **pad)
        root.rowconfigure(8, weight=1)

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
            t0 = parse_time(self.v_start.get())
            t1 = parse_time(self.v_stop.get())
        except ValueError:
            messagebox.showerror(APP_NAME, "Trim times must look like 45, 1:30 or 0:01:30.")
            return
        if t0 is not None and t1 is not None and t0 >= t1:
            messagebox.showerror(APP_NAME, "Trim stop must be after trim start.")
            return
        try:
            cfg = {"overlap": int(self.v_overlap.get()),
                   "height": int(self.v_height.get()),
                   "crf": int(self.v_crf.get()),
                   "encoder": self.v_enc.get(),
                   "force": self.v_force.get(),
                   "join": self.v_join.get(),
                   "t0": t0, "t1": t1}
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

    def _run(self, cmd):
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.PIPE, text=True,
                                         creationflags=NO_WINDOW)
            _, err = self.proc.communicate()
            return self.proc.returncode, err or ""
        except Exception as e:
            return 1, str(e)

    def _bump(self):
        self._n += 1
        self.q.put(("progress", self._n, self._total))

    def _do_unit(self, segs, pieces, layout, out, cfg):
        """Encode the needed pieces, then concat if there is more than one."""
        q = self.q
        out.parent.mkdir(parents=True, exist_ok=True)
        if len(pieces) == 1:
            i, ss, ln = pieces[0]
            q.put(("status", f"Encoding {segs[i][0]}  ({self._n + 1}/{self._total})"))
            rc, err = self._run(build_cmd(self.ffmpeg, segs[i][1], layout, out, cfg, ss, ln))
            self._bump()
            return rc == 0, err
        with tempfile.TemporaryDirectory(dir=out.parent, prefix=".tmp_") as tmp:
            parts = []
            for j, (i, ss, ln) in enumerate(pieces):
                if self.stop.is_set():
                    return False, "cancelled"
                q.put(("status", f"Encoding {segs[i][0]}  ({self._n + 1}/{self._total})"))
                part = Path(tmp) / f"p{j:04d}.mp4"
                rc, err = self._run(build_cmd(self.ffmpeg, segs[i][1], layout, part, cfg, ss, ln))
                self._bump()
                if rc != 0:
                    return False, err
                parts.append(part)
            if self.stop.is_set():
                return False, "cancelled"
            q.put(("status", "Joining pieces…"))
            lst = Path(tmp) / "list.txt"
            lst.write_text("".join("file '%s'\n" % p.as_posix().replace("'", "'\\''")
                                   for p in parts), encoding="utf-8")
            rc, err = self._run(concat_cmd(self.ffmpeg, lst, out))
            return rc == 0, err

    def _work(self, src, out_root, layout, cfg, recursive):
        q = self.q
        groups = scan(src, recursive)
        if not groups:
            q.put(("log", "No Tesla-named .mp4 files found."))
            q.put(("done", 0, 0))
            return

        # keep only timestamps that have every chosen camera
        by_parent, skipped = defaultdict(dict), 0
        for (parent, ts), files in sorted(groups.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
            missing = [c for c in layout if c not in files]
            if missing:
                q.put(("log", f"skip {ts}: missing {', '.join(missing)}"))
                skipped += 1
            else:
                by_parent[parent][ts] = files

        # build units: one joined event, or one single segment
        q.put(("status", "Reading clip lengths…"))
        units = []  # (parent, segs)
        for parent, tsmap in sorted(by_parent.items(), key=lambda kv: str(kv[0])):
            segs = []
            for ts in sorted(tsmap):
                if self.stop.is_set():
                    q.put(("done", 0, skipped))
                    return
                segs.append((ts, tsmap[ts], get_duration(self.ffmpeg, tsmap[ts][layout[0]])))
            if cfg["join"]:
                cur = []
                for seg in segs:
                    if cur:
                        gap = (ts_to_dt(seg[0]) - ts_to_dt(cur[-1][0])).total_seconds()
                        if gap > cur[-1][2] + 5:  # not contiguous -> new event
                            units.append((parent, cur))
                            cur = []
                    cur.append(seg)
                if cur:
                    units.append((parent, cur))
            else:
                units += [(parent, [seg]) for seg in segs]

        # plan pieces for each unit
        plan = []
        for parent, segs in units:
            pieces, total = make_pieces(segs, cfg["t0"], cfg["t1"])
            where = parent.relative_to(src) if recursive else Path(".")
            if cfg["join"]:
                q.put(("log", f"{segs[0][0]}  [{where}]: {len(segs)} clips, {fmt_time(total)} total"))
            if not pieces:
                q.put(("log", f"skip {segs[0][0]}: trim range is outside the video ({fmt_time(total)})"))
                skipped += 1
                continue
            plan.append((parent, segs, pieces))

        self._n, self._total = 0, sum(len(p) for _, _, p in plan)
        q.put(("total", self._total))
        done = 0
        for parent, segs, pieces in plan:
            if self.stop.is_set():
                break
            rel = parent.relative_to(src) if recursive else Path()
            kind = "joined" if cfg["join"] else "stitched"
            out = out_root / rel / f"{segs[0][0]}-{kind}.mp4"
            if out.exists() and not cfg["force"]:
                q.put(("log", f"exists {out.name}"))
                self._n += len(pieces)
                q.put(("progress", self._n, self._total))
                continue
            ok, err = self._do_unit(segs, pieces, layout, out, cfg)
            if self.stop.is_set():
                out.unlink(missing_ok=True)
                break
            if ok:
                done += 1
                q.put(("log", f"ok    {out.name}"))
            else:
                skipped += 1
                out.unlink(missing_ok=True)
                q.put(("log", f"ERROR {segs[0][0]}: {err.strip()[-300:]}"))
        q.put(("done", done, skipped))

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
                    self.lbl.configure(text=f"{state}: {m[1]} created, {m[2]} skipped/failed")
                    self.log(f"{state}. {m[1]} created, {m[2]} skipped/failed.")
                    self.btn_start.state(["!disabled"])
                    self.btn_cancel.state(["disabled"])
        except queue.Empty:
            pass
        self.after(100, self._poll)


if __name__ == "__main__":
    App().mainloop()
