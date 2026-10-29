#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ecad2step_gui.py — ECAD(IPC-2581 XML) -> 精确铜层 3D -> STEP 图形界面
=====================================================================

双击运行，或：
    python ecad2step_gui.py

只依赖 ecad2step.py 同目录、tkinter（Python 自带）、OCP、shapely、lxml。
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import ecad2step as core

APP_TITLE = "ECAD → STEP 精确铜层建模"


def _base_dir() -> Path:
    """源码运行用脚本目录；PyInstaller 打包后用解包目录。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent


BASE_DIR = _base_dir()
CONFIG_PATH = Path(os.getenv("APPDATA", str(Path.home()))) / "ecad2step" / "gui.json"
ASSETS_DIR = BASE_DIR / "assets"
ICON_ICO = ASSETS_DIR / "ecad2step.ico"
ICON_PNG = ASSETS_DIR / "ecad2step.png"

QUALITY_CHOICES = [
    ("精确 (慢)", "ultra"),
    ("标准 (推荐)", "medium"),
    ("快速", "fast"),
]

# 精度档位 -> 默认简化容差 mm
QUALITY_SIMPLIFY = {"ultra": 0.0, "medium": 0.01, "fast": 0.02}


class QueueWriter:
    """把 print 输出送进 GUI 队列。"""

    def __init__(self, q: queue.Queue):
        self.q = q
        self.buf = ""

    def write(self, s):
        if not s:
            return
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.q.put(("log", line))

    def flush(self):
        if self.buf:
            self.q.put(("log", self.buf))
            self.buf = ""


class Cancelled(Exception):
    pass


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.start_time = 0.0
        self.last_output: Path | None = None
        self._icon_photo = None

        self.var_input = tk.StringVar()
        self.var_output = tk.StringVar()
        self.var_quality = tk.StringVar(value="标准 (推荐)")
        self.var_fill = tk.BooleanVar(value=False)
        self.var_copper_only = tk.BooleanVar(value=False)
        self.var_drills = tk.BooleanVar(value=True)
        self.var_vias = tk.BooleanVar(value=False)
        self.var_split = tk.BooleanVar(value=False)
        self._fill_before_cu = False
        self.var_split_dir = tk.StringVar()
        self.var_min_area = tk.StringVar(value="0.001")
        self.var_status = tk.StringVar(value="就绪")

        self._load_config()
        self._build_ui()
        self.root.after(100, self._poll)

    # ---- 界面 -------------------------------------------------------------
    def _build_ui(self):
        self.root.title(APP_TITLE)
        self.root.geometry("880x640")
        self.root.minsize(780, 560)
        self._set_window_icon()

        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(self.root)
        frm.pack(fill="x", **pad)

        # 文件行
        row = 0
        ttk.Label(frm, text="ECAD XML").grid(row=row, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.var_input).grid(
            row=row, column=1, columnspan=3, sticky="ew", padx=6)
        ttk.Button(frm, text="浏览…", command=self._pick_input, width=8).grid(
            row=row, column=4)
        row += 1
        ttk.Label(frm, text="输出 STEP").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=self.var_output).grid(
            row=row, column=1, columnspan=3, sticky="ew", padx=6, pady=4)
        ttk.Button(frm, text="浏览…", command=self._pick_output, width=8).grid(
            row=row, column=4, pady=4)

        frm.columnconfigure(1, weight=1)

        # 选项
        opt = ttk.LabelFrame(self.root, text=" 建模选项 ")
        opt.pack(fill="x", padx=8, pady=(2, 6))

        ttk.Label(opt, text="精度").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        cb = ttk.Combobox(opt, textvariable=self.var_quality, state="readonly",
                          values=[x[0] for x in QUALITY_CHOICES], width=14)
        cb.grid(row=0, column=1, sticky="w", pady=4)
        cb.bind("<<ComboboxSelected>>", self._quality_changed)

        self.cb_fill = ttk.Checkbutton(
            opt, text="铜层间隙填充 FR4（更真实；约 3–5 分钟，文件更大）",
            variable=self.var_fill)
        self.cb_fill.grid(row=0, column=2, columnspan=2, sticky="w", padx=16)
        ttk.Checkbutton(opt, text="扣除钻孔", variable=self.var_drills).grid(
            row=1, column=0, columnspan=2, sticky="w", padx=6)
        ttk.Checkbutton(opt, text="电镀孔做成实心铜柱", variable=self.var_vias).grid(
            row=1, column=2, sticky="w", padx=16)

        ttk.Label(opt, text="最小铜岛 mm²").grid(row=1, column=3, sticky="e")
        ttk.Entry(opt, textvariable=self.var_min_area, width=8).grid(
            row=1, column=4, sticky="w", padx=(4, 8))

        ttk.Checkbutton(opt, text="只生成铜层（不生成介质层）",
                        variable=self.var_copper_only,
                        command=self._toggle_copper_only).grid(
            row=2, column=0, columnspan=2, sticky="w", padx=6, pady=(2, 4))

        ttk.Checkbutton(opt, text="同时分层导出", variable=self.var_split,
                        command=self._toggle_split).grid(
            row=3, column=0, sticky="w", padx=6, pady=(2, 4))
        self.entry_split = ttk.Entry(opt, textvariable=self.var_split_dir,
                                     state="disabled")
        self.entry_split.grid(row=3, column=1, columnspan=3, sticky="ew", padx=6)
        self.btn_split = ttk.Button(opt, text="目录…", width=8, state="disabled",
                                    command=self._pick_split_dir)
        self.btn_split.grid(row=3, column=4, padx=(0, 8))

        # 配置里记住了“仅铜层”时，启动即关闭 FR4 选项
        if self.var_copper_only.get():
            self.var_fill.set(False)
            self.cb_fill.configure(state="disabled")

        # 操作
        act = ttk.Frame(self.root)
        act.pack(fill="x", padx=8, pady=2)
        self.btn_start = ttk.Button(act, text="开始生成", command=self._start)
        self.btn_start.pack(side="left")
        self.btn_cancel = ttk.Button(act, text="取消", command=self._cancel,
                                     state="disabled")
        self.btn_cancel.pack(side="left", padx=6)
        self.btn_open = ttk.Button(act, text="打开输出目录", command=self._open_dir,
                                   state="disabled")
        self.btn_open.pack(side="left", padx=6)
        ttk.Button(act, text="检查文件", command=self._inspect).pack(side="left", padx=6)

        self.progress = ttk.Progressbar(self.root, mode="determinate", maximum=100)
        self.progress.pack(fill="x", padx=8, pady=(6, 2))
        ttk.Label(self.root, textvariable=self.var_status, anchor="w").pack(
            fill="x", padx=10)

        # 日志
        self.log = ScrolledText(self.root, height=18, wrap="word",
                                font=("Consolas", 9))
        self.log.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        self.log.configure(state="disabled")

        self._log("提示：生成的 STEP 单位为 mm；Abaqus 导入时单位选 mm。")
        self._log("首次运行会加载 OpenCASCADE，约 3–5 秒。")

    def _set_window_icon(self):
        """窗口/任务栏图标：优先用 PNG（缩放更清晰），失败再退回 ICO。"""
        if ICON_PNG.exists():
            try:
                self._icon_photo = tk.PhotoImage(file=str(ICON_PNG))
                self.root.iconphoto(True, self._icon_photo)
                return
            except Exception:
                self._icon_photo = None
        if sys.platform == "win32" and ICON_ICO.exists():
            try:
                self.root.iconbitmap(str(ICON_ICO))
                self.root.iconbitmap(default=str(ICON_ICO))
            except Exception:
                pass

    # ---- 配置 -------------------------------------------------------------
    def _load_config(self):
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
        self.var_input.set(cfg.get("input", ""))
        self.var_output.set(cfg.get("output", ""))
        q = cfg.get("quality", "标准 (推荐)")
        if q in [x[0] for x in QUALITY_CHOICES]:
            self.var_quality.set(q)
        fill_cfg = bool(cfg.get("fill", False))
        self._fill_before_cu = fill_cfg
        self.var_fill.set(fill_cfg)
        self.var_copper_only.set(bool(cfg.get("copper_only", False)))
        self.var_drills.set(bool(cfg.get("drills", True)))
        self.var_vias.set(bool(cfg.get("vias", False)))
        self.var_split.set(bool(cfg.get("split", False)))
        self.var_split_dir.set(cfg.get("split_dir", ""))
        self.var_min_area.set(str(cfg.get("min_area", "0.001")))

    def _save_config(self):
        cfg = {
            "input": self.var_input.get(),
            "output": self.var_output.get(),
            "quality": self.var_quality.get(),
            "fill": (self._fill_before_cu if self.var_copper_only.get()
                     else self.var_fill.get()),
            "copper_only": self.var_copper_only.get(),
            "drills": self.var_drills.get(),
            "vias": self.var_vias.get(),
            "split": self.var_split.get(),
            "split_dir": self.var_split_dir.get(),
            "min_area": self.var_min_area.get(),
        }
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        except Exception:
            pass

    # ---- 事件 -------------------------------------------------------------
    def _pick_input(self):
        p = filedialog.askopenfilename(
            title="选择 ECAD 导出文件",
            filetypes=[("IPC-2581 XML", "*.xml *.cvg"), ("所有文件", "*.*")])
        if not p:
            return
        self.var_input.set(p)
        if not self.var_output.get().strip():
            src = Path(p)
            self.var_output.set(str(src.with_name(src.stem + "_3D.step")))
        self._inspect()

    def _pick_output(self):
        p = filedialog.asksaveasfilename(
            title="保存 STEP", defaultextension=".step",
            filetypes=[("STEP", "*.step *.stp")])
        if p:
            self.var_output.set(p)

    def _pick_split_dir(self):
        p = filedialog.askdirectory(title="选择分层导出目录")
        if p:
            self.var_split_dir.set(p)

    def _toggle_split(self):
        st = "normal" if self.var_split.get() else "disabled"
        self.btn_split.configure(state=st)
        self.entry_split.configure(state=st)

    def _toggle_copper_only(self):
        if self.var_copper_only.get():
            self._fill_before_cu = self.var_fill.get()
            self.var_fill.set(False)
            self.cb_fill.configure(state="disabled")
            self.var_status.set("仅生成铜层：介质层与 FR4 填充已关闭。")
        else:
            self.cb_fill.configure(state="normal")
            self.var_fill.set(self._fill_before_cu)
            self.var_status.set("生成铜层 + 介质层。")

    def _quality_changed(self, _evt=None):
        name = self.var_quality.get()
        q = dict(QUALITY_CHOICES).get(name, "medium")
        self.var_status.set(f"精度：{name}，简化容差 {QUALITY_SIMPLIFY[q]:g} mm")

    def _inspect(self):
        path = self.var_input.get().strip()
        if not path:
            self._log("请先选择 XML 文件。")
            return
        if self.worker and self.worker.is_alive():
            return
        self._set_busy(True, inspect_only=True)
        self.worker = threading.Thread(target=self._inspect_worker, args=(path,),
                                       daemon=True)
        self.worker.start()

    def _inspect_worker(self, path):
        old = sys.stdout
        sys.stdout = QueueWriter(self.q)
        try:
            args = SimpleNamespace(units=None, thickness=None, simplify=0.0,
                                   min_area=0.0, max_drills=2000, debug=False,
                                   dump=False, drills=True, force_drills=False,
                                   detail="copper", no_fill=True, copper_only=False,
                                   vias=False,
                                   output=str(self.var_output.get()), format="step",
                                   split=None, tolerance=0.02, cmd="inspect",
                                   quality="high")
            core.set_quality("high")
            model = core.load_model(path, args)
            core.print_report(model, args)
            self.q.put(("status", "检查完成，可以开始生成。"))
        except Exception as e:
            self.q.put(("log", f"[错误] {e}"))
            if "--debug" in sys.argv:
                self.q.put(("log", traceback.format_exc()))
        finally:
            sys.stdout = old
            self.q.put(("busy", False))

    def _start(self):
        if self.worker and self.worker.is_alive():
            return
        src = self.var_input.get().strip()
        out = self.var_output.get().strip()
        if not src or not Path(src).exists():
            messagebox.showwarning(APP_TITLE, "请选择有效的 ECAD XML 文件。")
            return
        if not out:
            messagebox.showwarning(APP_TITLE, "请指定输出的 STEP 路径。")
            return
        if self.var_split.get() and not self.var_split_dir.get().strip():
            messagebox.showwarning(APP_TITLE, "勾选分层导出后，请选择输出目录。")
            return
        try:
            min_area = float(self.var_min_area.get() or 0)
        except ValueError:
            messagebox.showwarning(APP_TITLE, "最小铜岛面积必须是数字。")
            return

        quality = dict(QUALITY_CHOICES).get(self.var_quality.get(), "medium")
        simplify = QUALITY_SIMPLIFY.get(quality, 0.01)

        args = SimpleNamespace(
            input=src, output=out, units=None, thickness=None,
            simplify=simplify, min_area=max(0.0, min_area),
            max_drills=100000, debug=False, dump=False,
            drills=self.var_drills.get(), force_drills=False,
            detail="copper",
            no_fill=not self.var_fill.get() or self.var_copper_only.get(),
            copper_only=self.var_copper_only.get(),
            vias=self.var_vias.get(), format="step",
            split=self.var_split_dir.get().strip() if self.var_split.get() else None,
            tolerance=0.02, cmd="convert", quality=quality,
        )
        self._save_config()
        self.cancel_event.clear()
        self.last_output = Path(out)
        self.btn_open.configure(state="disabled")
        self._set_busy(True)
        self.progress.configure(mode="determinate", value=0)
        self._log("=" * 70)
        self._log(f"开始生成：{Path(src).name}")
        fill_txt = "不适用（仅铜层）" if args.copper_only else (
            "否" if args.no_fill else "是")
        self._log(f"精度 {self.var_quality.get()}，简化 {simplify:g} mm，"
                  f"输出：{'仅铜层' if args.copper_only else '铜层 + 介质层'}，"
                  f"填充 FR4：{fill_txt}")
        self.worker = threading.Thread(target=self._convert_worker, args=(args,),
                                       daemon=True)
        self.worker.start()

    def _convert_worker(self, args):
        old_out, old_err = sys.stdout, sys.stderr
        writer = QueueWriter(self.q)
        sys.stdout = writer
        sys.stderr = writer
        t0 = time.time()
        try:
            core.set_quality(args.quality)
            model = core.load_model(args.input, args)
            if self.cancel_event.is_set():
                raise Cancelled()
            core.print_report(model, args)

            def progress(i, n, msg):
                if self.cancel_event.is_set():
                    raise Cancelled()
                self.q.put(("progress", (i, n, msg)))

            compound, per_band = core.build_3d(model, args, progress=progress)
            if self.cancel_event.is_set():
                raise Cancelled()
            self.q.put(("status", "正在写出 STEP …"))
            self.q.put(("indeterminate", True))
            core.write_outputs(model, compound, per_band, args)
            size = Path(args.output).stat().st_size / 1e6
            self.q.put(("done", (str(Path(args.output)), size, time.time() - t0)))
        except Cancelled:
            self.q.put(("log", "已取消。"))
            self.q.put(("cancelled", None))
        except Exception as e:
            self.q.put(("log", f"[错误] {e}"))
            self.q.put(("log", traceback.format_exc()))
            self.q.put(("failed", str(e)))
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            self.q.put(("busy", False))

    def _cancel(self):
        if self.worker and self.worker.is_alive():
            self.cancel_event.set()
            self.var_status.set("正在取消（当前图层完成后停止）…")

    def _open_dir(self):
        target = self.last_output.parent if self.last_output else None
        if target and target.exists():
            try:
                os.startfile(str(target))  # Windows
            except AttributeError:
                import subprocess
                subprocess.Popen(["xdg-open", str(target)])

    # ---- 队列 -------------------------------------------------------------
    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "status":
                    self.var_status.set(payload)
                elif kind == "progress":
                    i, n, msg = payload
                    self.progress.configure(mode="determinate",
                                            maximum=max(n, 1), value=i)
                    self.var_status.set(msg)
                elif kind == "indeterminate":
                    if payload:
                        self.progress.configure(mode="indeterminate")
                        self.progress.start(12)
                    else:
                        self.progress.stop()
                        self.progress.configure(mode="determinate")
                elif kind == "done":
                    path, size, secs = payload
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=100)
                    self.var_status.set(f"完成：{Path(path).name}，{size:.1f} MB，"
                                        f"{secs:.0f} 秒")
                    self.btn_open.configure(state="normal")
                    self._log(f"\n完成：{path}  ({size:.1f} MB, {secs:.0f}s)")
                    self._log("Abaqus 导入：File > Import > Part > STEP，单位选 mm，"
                              "并勾选 Combine Bodies（合成一个 Part、多个 cell）。")
                    messagebox.showinfo(APP_TITLE,
                                        f"生成完成\n\n{path}\n{size:.1f} MB\n"
                                        f"耗时 {secs:.0f} 秒")
                elif kind == "cancelled":
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
                    self.var_status.set("已取消")
                elif kind == "failed":
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
                    self.var_status.set("失败，详见日志")
                    messagebox.showerror(APP_TITLE, f"生成失败：{payload}")
                elif kind == "busy":
                    self._set_busy(bool(payload))
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _set_busy(self, busy: bool, inspect_only: bool = False):
        self.btn_start.configure(state="disabled" if busy else "normal")
        self.btn_cancel.configure(state="normal" if busy and not inspect_only
                                  else "disabled")
        if not busy:
            self.progress.stop()
            self.progress.configure(mode="determinate")

    def _log(self, text: str):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")


def _selftest() -> int:
    """打包依赖自检：用示例板跑完整解析/建模/STEP 导出，不打开 GUI。"""
    import tempfile

    outdir = Path(tempfile.gettempdir()) / "ecad2step_selftest"
    report_path = Path(tempfile.gettempdir()) / "ecad2step_selftest.json"
    result = {"ok": False, "error": ""}
    try:
        outdir.mkdir(parents=True, exist_ok=True)
        log = open(outdir / "selftest.log", "w", encoding="utf-8")
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = log
        try:
            args = SimpleNamespace(
                input=str(BASE_DIR / "samples" / "demo_board.xml"),
                output=str(outdir / "demo.step"), units=None, thickness=None,
                simplify=0.02, min_area=0.0, max_drills=100000, debug=False,
                dump=False, drills=True, force_drills=False, detail="copper",
                no_fill=True, copper_only=False, vias=False, format="step",
                split=None, tolerance=0.02, cmd="convert", quality="fast")
            core.set_quality("fast")
            model = core.load_model(args.input, args)
            compound, per_band = core.build_3d(model, args)
            core.write_outputs(model, compound, per_band, args)
            result.update(
                ok=True,
                size=Path(args.output).stat().st_size,
                bands=[(label, len(bs)) for label, bs in per_band])
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            log.close()
    except Exception:
        result["error"] = traceback.format_exc()
    try:
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except Exception:
        pass
    return 0 if result["ok"] else 2


def main():
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    # Windows 高 DPI，避免字体发虚
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.25)
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
