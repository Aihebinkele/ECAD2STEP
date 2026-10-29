#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ecad2step.py — ECAD 数据 -> 精确铜层 3D 实体 -> STEP / STL
=========================================================

支持输入
--------
1. IPC-2581 XML（*.xml / *.cvg）—— 首选。包含每层铜箔的走线、焊盘、铜面、
   板框、叠层，可生成“全 3D 金属化”模型（等价 ANSA ECAD Importer Level 5）。
2. IDF 中性文件（*.emn + *.emp）—— 只有板框、钻孔、器件位置，**没有铜箔图形**，
   只能生成板块/板框模型，无法生成精确铜层。

用法
----
    # 1) 先体检：看单位、层、叠层、铜特征数量，不建 3D
    python ecad2step.py inspect board.xml

    # 2) 生成 STEP（推荐给 Abaqus）
    python ecad2step.py convert board.xml -o board.step

    # 3) 生成 STL（只建议预览/3D 打印）
    python ecad2step.py convert board.xml -o board.stl --format stl

    # 4) 每层单独一个 STEP（方便在 Abaqus 里逐层赋材料）
    python ecad2step.py convert board.xml -o board.step --split out_dir

    # 5) 只看板框/叠层，不建铜
    python ecad2step.py convert board.xml -o board.step --detail board

关键参数
--------
    --detail copper|board|simple    copper=全铜层(默认), board=只有介质层, simple=整板块
    --format step|stl               step 为 B-rep 实体（Abaqus 首选）；stl 为三角面片
    --split DIR                     每层另外单独导出 STEP
    --no-drills                     不裁钻孔
    --vias                          把电镀孔做成实心铜柱（热分析常用等效）
    --simplify TOL                  铜多边形简化容差 mm（大板提速）
    --min-area MM2                  丢弃小于该面积的铜岛（默认 0）
    --quality ultra|high|medium|fast 离散精度，直接决定 STEP 大小/导出时间
    --thickness MM                  叠层缺失时强制板厚
    --units mm|inch                 强制输入单位

输出说明
--------
* 铜层与介质层在 Z 向按叠层顺序**不重叠**排列，相邻实体共面，可直接在 Abaqus 中
  用 Tie / Merge 连接，或分别赋材料后做热-结构分析。
* 铜层带内的非铜区域用“板框 - 铜”自动补介质（conformal fill），FR4 在走线之间
  是连续的，不会出现空气隙；如只要速度快可用 --no-fill。
* 同一铜层内的所有铜岛保留为独立 cell（不做 3D 布尔并集），网格质量更好、速度快。

依赖：OCP (OpenCASCADE), shapely, lxml, numpy
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import sys
import time
import traceback
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import numpy as np
from lxml import etree
from shapely import affinity

# 圆/圆弧离散精度。STEP 的面数 ≈ 顶点数，直接决定文件大小和导出时间。
# 注意：shapely 的 quad_segs 是“每 1/4 圆的段数”，circle=8 → 32 边形。
QUALITY_PRESETS = {
    "ultra":  {"circle": 12, "obround": 8, "stroke": 8, "arc": 5.0},
    "high":   {"circle": 8,  "obround": 6, "stroke": 6, "arc": 8.0},
    "medium": {"circle": 6,  "obround": 4, "stroke": 4, "arc": 12.0},
    "fast":   {"circle": 4,  "obround": 3, "stroke": 3, "arc": 15.0},
}
TES = dict(QUALITY_PRESETS["high"])


def set_quality(name: str):
    TES.update(QUALITY_PRESETS.get(name, QUALITY_PRESETS["high"]))
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPolygon,
    Point,
    Polygon,
    box,
)
from shapely.ops import unary_union
from shapely.validation import make_valid

# Windows 控制台默认 GBK，中文/符号会崩
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---------------------------------------------------------------------------
# 单位
# ---------------------------------------------------------------------------
UNIT_TO_MM = {
    "MILLIMETER": 1.0, "MM": 1.0, "MMETER": 1.0,
    "CENTIMETER": 10.0, "CM": 10.0,
    "METER": 1000.0, "M": 1000.0,
    "MICRON": 0.001, "UM": 0.001, "MICROMETER": 0.001,
    "INCH": 25.4, "IN": 25.4, "INCHES": 25.4,
    "MIL": 0.0254, "THOU": 0.0254,
}


def unit_factor(units: Optional[str], fallback: float = 1.0) -> float:
    if not units:
        return fallback
    return UNIT_TO_MM.get(str(units).strip().upper(), fallback)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def lname(el) -> str:
    """去掉命名空间后的标签名。"""
    tag = el.tag
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def get(el, *names, default=None):
    """按多个候选属性名取属性（不同导出器大小写/命名不一致）。"""
    for n in names:
        v = el.get(n)
        if v is not None:
            return v
    return default


def fget(el, *names, default=0.0) -> float:
    v = get(el, *names)
    if v is None:
        return default
    return fnum(v, default)


def fnum(v, default=0.0) -> float:
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if not s:
        return default
    try:
        return float(s)
    except ValueError:
        m = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", s)
        return float(m.group(0)) if m else default


def children(el, *names) -> list:
    if el is None:
        return []
    wanted = set(names)
    return [c for c in el if lname(c) in wanted]


def first_child(el, *names):
    for c in el:
        if lname(c) in names:
            return c
    return None


def polygonal(g):
    """把任意 shapely 几何规整成 Polygon/MultiPolygon。"""
    if g is None:
        return None
    try:
        if g.is_empty:
            return None
        if not g.is_valid:
            g = make_valid(g)
        if isinstance(g, (Polygon, MultiPolygon)):
            return g if not g.is_empty else None
        if isinstance(g, GeometryCollection):
            polys = []
            for x in g.geoms:
                if isinstance(x, (Polygon, MultiPolygon)):
                    polys.append(x)
            if not polys:
                return None
            u = unary_union(polys)
            return u if not u.is_empty else None
    except Exception:
        return None
    return None


def iter_polygons(g) -> Iterable[Polygon]:
    """展开 Polygon / MultiPolygon。"""
    if g is None or g.is_empty:
        return
    if isinstance(g, Polygon):
        yield g
    elif isinstance(g, MultiPolygon):
        for p in g.geoms:
            yield p


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass
class LayerInfo:
    name: str
    func: str = ""
    side: str = ""
    copper: bool = False
    order: int = 0


@dataclass
class Band:
    """叠层中的一个物理层（铜或介质），z 单位 mm。"""
    kind: str              # copper | dielectric
    name: str
    material: str = ""
    thickness: float = 0.0
    ref: str = ""          # 对应 ECAD 层名
    z0: float = 0.0
    z1: float = 0.0

    @property
    def is_copper(self) -> bool:
        return self.kind == "copper"


@dataclass
class BoardModel:
    fmt: str = ""
    source: str = ""
    units: str = "MILLIMETER"
    outline: Any = None                    # shapely Polygon
    holes: list = field(default_factory=list)   # (x, y, d, plated) mm
    layers: list = field(default_factory=list)
    copper: dict = field(default_factory=dict)  # name -> shapely geom (mm)
    bands: list = field(default_factory=list)   # bottom -> top
    components: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def thickness(self) -> float:
        if not self.bands:
            return 0.0
        return max(b.z1 for b in self.bands)

    @property
    def copper_layers(self) -> list:
        return [b for b in self.bands if b.is_copper]

    def summary(self) -> dict:
        return {
            "format": self.fmt,
            "units": self.units,
            "bands": len(self.bands),
            "copper_layers": len(self.copper_layers),
            "thickness_mm": round(self.thickness, 4),
            "holes": len(self.holes),
            "components": len(self.components),
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# 2D 图元
# ---------------------------------------------------------------------------
def circle(cx: float, cy: float, d: float) -> Optional[Polygon]:
    if d <= 0:
        return None
    return Point(cx, cy).buffer(d / 2.0, quad_segs=TES["circle"])


def rect(cx: float, cy: float, w: float, h: float, rot: float = 0.0) -> Optional[Polygon]:
    if w <= 0 or h <= 0:
        return None
    g = box(cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0)
    if rot:
        g = affinity.rotate(g, rot, origin=(cx, cy))
    return g


def obround(cx: float, cy: float, w: float, h: float, rot: float = 0.0) -> Optional[Polygon]:
    if w <= 0 or h <= 0:
        return None
    if abs(w - h) < 1e-9:
        return circle(cx, cy, w)
    if w > h:
        r = h / 2.0
        line = LineString([(cx - (w - h) / 2.0, cy), (cx + (w - h) / 2.0, cy)])
    else:
        r = w / 2.0
        line = LineString([(cx, cy - (h - w) / 2.0), (cx, cy + (h - w) / 2.0)])
    g = line.buffer(r, cap_style="round", join_style="round", quad_segs=TES["obround"])
    if rot:
        g = affinity.rotate(g, rot, origin=(cx, cy))
    return g


def _arc_points(p0, p1, center, clockwise: bool, max_step_deg: float = None):
    """从 p0 沿圆弧到 p1，返回不含 p0、含 p1 的点列。"""
    if max_step_deg is None:
        max_step_deg = TES["arc"]
    if center is None:
        return [p1]
    cx, cy = center
    r0 = math.hypot(p0[0] - cx, p0[1] - cy)
    r1 = math.hypot(p1[0] - cx, p1[1] - cy)
    if r0 < 1e-12 or r1 < 1e-12:
        return [p1]
    a0 = math.atan2(p0[1] - cy, p0[0] - cx)
    a1 = math.atan2(p1[1] - cy, p1[0] - cx)
    if clockwise:
        sweep = -((a0 - a1) % (2 * math.pi))
        if sweep > -1e-9:
            sweep = -2 * math.pi
    else:
        sweep = (a1 - a0) % (2 * math.pi)
        if sweep < 1e-9:
            sweep = 2 * math.pi
    n = max(3, int(abs(math.degrees(sweep)) / max_step_deg) + 1)
    pts = []
    for i in range(1, n + 1):
        a = a0 + sweep * i / n
        rr = r0 + (r1 - r0) * i / n
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    pts[-1] = p1
    return pts


def stroke(points: list, width: float, cap: str = "round") -> Optional[Polygon]:
    """把折线按线宽“描粗”成面。"""
    if len(points) < 2 or width <= 0:
        return None
    cap_style = {"round": "round", "square": "square", "flat": "flat",
                 "butt": "flat"}.get(str(cap).lower(), "round")
    try:
        return LineString(points).buffer(
            width / 2.0, cap_style=cap_style, join_style="round", quad_segs=TES["stroke"]
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# IPC-2581 解析
# ---------------------------------------------------------------------------
COPPER_FUNCS = {"SIGNAL", "PLANE", "MIXED", "CONDUCTOR", "POWER", "GROUND"}

PRIMITIVE_TAGS = {
    "Circle", "Rect", "RectCenter", "Obround", "Line", "Polygon", "Contour",
    "Donut", "Thermal", "Diamond", "Octagon", "Triangle", "Arc", "Moire",
}

SHAPE_TAGS = {
    "Pad", "LayerPad", "Line", "Polyline", "Contour", "Rect", "Circle",
    "Arc", "Polygon", "Donut", "Thermal", "Obround", "Diamond", "Octagon",
    "Triangle", "Text", "Hole", "Slot", "SlotCavity", "Fiducial",
}

CONTAINER_TAGS = {"Set", "Features", "Feature", "PadStack", "ColorGroup", "PadStackDef"}


class Ipc2581Parser:
    def __init__(self, path: str, force_units: Optional[str] = None,
                 simplify: float = 0.0, min_area: float = 0.0):
        self.path = path
        self.force_units = force_units
        self.simplify = simplify
        self.min_area = min_area
        self.k = 1.0
        self.warnings: list[str] = []
        self.tag_counts = Counter()
        self.unknown_tags = Counter()
        self.feature_counts = Counter()
        self.prims: dict[str, Any] = {}
        self.padstacks: dict[str, list] = {}
        self.copper: dict[str, Any] = {}
        self.copper_names: set[str] = set()
        self.units: str = "MILLIMETER"
        self.layer_by_name: dict[str, LayerInfo] = {}
        self.rot_cache: dict[tuple, Any] = {}
        self.neg_geoms: dict[str, list] = defaultdict(list)
        self.drills: list[tuple] = []          # (x, y, d, plated)
        self._steps: list = []
        self._layer_refs: set[str] = set()

    # ---- 顶层 -------------------------------------------------------------
    def parse(self) -> BoardModel:
        parser = etree.XMLParser(huge_tree=True, recover=True, remove_blank_text=False)
        tree = etree.parse(self.path, parser)
        root = tree.getroot()

        for el in root.iter():
            if isinstance(el.tag, str):
                self.tag_counts[lname(el)] += 1

        units = None
        for el in root.iter():
            if lname(el) in ("CadHeader", "DictionaryStandard"):
                units = get(el, "units", "unit")
                if units:
                    break
        if self.force_units:
            units = self.force_units
        self.units = units or "MILLIMETER"
        self.k = unit_factor(self.units)

        self._collect_layers(root)
        self._collect_primitives(root)
        self._collect_padstacks(root)

        outline = self._parse_outline(root)
        self._parse_copper(root)

        # 板框兜底
        if outline is None:
            outline = self._fallback_outline()
            if outline is not None:
                self.warnings.append("未找到 Profile/板框，已用铜层包围盒代替板框。")
        self.outline = outline

        # 裁剪到板框
        if self.outline is not None:
            for name, g in list(self.copper.items()):
                try:
                    g = polygonal(g.intersection(self.outline))
                except Exception:
                    g = polygonal(g)
                self.copper[name] = g

        bands = self._build_bands(root)
        return BoardModel(
            fmt="IPC-2581",
            source=str(self.path),
            units=self.units,
            outline=self.outline,
            holes=self._dedupe_holes(self.drills),
            layers=list(self.layer_by_name.values()),
            copper=self.copper,
            bands=bands,
            warnings=self.warnings,
            stats={
                "tag_counts": dict(self.tag_counts.most_common(40)),
                "unknown_tags": dict(self.unknown_tags.most_common(20)),
                "features": dict(self.feature_counts),
                "primitives": len(self.prims),
            },
        )

    # ---- 层 ---------------------------------------------------------------
    def _collect_layers(self, root):
        order = 0
        seen = set()
        for el in root.iter():
            if lname(el) != "Layer":
                continue
            name = get(el, "name", "layerName")
            if not name or name in seen:
                continue
            seen.add(name)
            func = (get(el, "layerFunction", "function", default="") or "").upper()
            side = (get(el, "side", default="") or "").upper()
            self.layer_by_name[name] = LayerInfo(
                name=name, func=func, side=side,
                copper=func in COPPER_FUNCS, order=order,
            )
            order += 1

    def _collect_primitives(self, root):
        for el in root.iter():
            tag = lname(el)
            if tag not in ("EntryStandard", "EntryUser", "StandardPrimitive",
                           "UserPrimitive", "StandardShape", "UserShape"):
                continue
            pid = get(el, "id", "standardPrimitiveId", "name")
            if not pid:
                continue
            shape = None
            for ch in el:
                if lname(ch) in PRIMITIVE_TAGS:
                    shape = ch
                    break
            if shape is not None:
                g = self._build_primitive(shape)
            elif tag in ("EntryUser", "UserPrimitive", "UserShape"):
                # EntryUser 的几何在 UserSpecial 里，需要逐子图元合成
                special = first_child(el, "UserSpecial", "userSpecial")
                g = self._user_special_geom(special if special is not None else el)
            else:
                g = None
            if g is not None:
                self.prims[pid] = g

    def _user_special_geom(self, el):
        """把 UserSpecial 下的 Arc/Line/Polyline 等合成一个填充面。"""
        geoms = []
        for ch in el:
            tag = lname(ch)
            g = None
            if tag == "Polygon":
                g = self._parse_polygon(ch)
            elif tag == "Contour":
                g = self._parse_contour(ch)
            elif tag == "Polyline":
                pts = self._polyline_points(ch)
                if len(pts) >= 3:
                    g = Polygon(pts)
                    if not g.is_valid:
                        g = polygonal(make_valid(g))
            elif tag == "Arc":
                # 无宽度的 Arc 是轮廓线，按扇形填充；两个半圆合成整圆
                cx = fget(ch, "centerX", default=math.nan)
                cy = fget(ch, "centerY", default=math.nan)
                if not (math.isnan(cx) or math.isnan(cy)):
                    p0 = (fget(ch, "startX") * self.k, fget(ch, "startY") * self.k)
                    p1 = (fget(ch, "endX") * self.k, fget(ch, "endY") * self.k)
                    cw = str(get(ch, "clockwise", default="false")).lower() in ("true", "1", "yes")
                    pts = [(cx * self.k, cy * self.k)]
                    pts += _arc_points(p0, p1, (cx * self.k, cy * self.k), cw)
                    if len(pts) >= 3:
                        g = polygonal(Polygon(pts))
            elif tag in PRIMITIVE_TAGS:
                g = self._build_primitive(ch)
            if g is not None:
                geoms.append(g)
        if not geoms:
            return None
        return polygonal(unary_union(geoms))

    def _collect_padstacks(self, root):
        """PadStackDef -> 每层使用的 StandardPrimitive 与偏移。"""
        for el in root.iter():
            if lname(el) != "PadStackDef":
                continue
            name = get(el, "name", "id")
            if not name:
                continue
            entries = []
            for pd in el.iter():
                if lname(pd) != "PadstackPadDef":
                    continue
                layer = get(pd, "layerRef", "layer", "layerName", default="") or ""
                usage = (get(pd, "padUse", "padUsage", default="REGULAR") or "REGULAR").upper()
                pid = ""
                for ref in pd.iter():
                    if lname(ref) in ("StandardPrimitiveRef", "StandardPrimitive",
                                      "UserPrimitiveRef", "UserPrimitive"):
                        pid = get(ref, "id", "standardPrimitiveId", default="") or ""
                        if pid:
                            break
                loc = first_child(pd, "Location", "location")
                dx = fget(loc, "x", "X") if loc is not None else 0.0
                dy = fget(loc, "y", "Y") if loc is not None else 0.0
                rot = fget(pd, "rotation", default=0.0)
                entries.append((layer.upper(), pid, dx * self.k, dy * self.k, rot, usage))
            self.padstacks[name] = entries

    # ---- 板框 -------------------------------------------------------------
    def _parse_outline(self, root):
        # 优先 Step/Profile
        profiles = [el for el in root.iter() if lname(el) == "Profile"]
        for prof in profiles:
            parent = prof.getparent()
            if parent is None or lname(parent) != "Step":
                continue
            g = self._profile_geom(prof)
            if g is not None:
                return g
        for prof in profiles:
            g = self._profile_geom(prof)
            if g is not None:
                return g
        # 部分导出器把板框放在 BoardOutline 层
        for name, g in self.copper.items():
            if re.search(r"(BOARD|OUTLINE|PROFILE)", name, re.I):
                gg = polygonal(g)
                if gg is not None:
                    self.warnings.append(f"板框取自层 {name}。")
                    return gg
        return None

    def _profile_geom(self, prof):
        polys = []
        for ch in prof:
            tag = lname(ch)
            if tag == "Polygon":
                p = self._parse_polygon(ch)
                if p is not None:
                    polys.append(p)
            elif tag == "Contour":
                p = self._parse_contour(ch)
                if p is not None:
                    polys.append(p)
            elif tag == "Cutout":
                continue
        # 有的 Profile 直接放 PolyBegin/PolyStep
        if not polys and any(lname(c).startswith("Poly") for c in prof):
            p = self._parse_polygon(prof)
            if p is not None:
                polys.append(p)
        if not polys:
            return None
        if len(polys) == 1:
            return polys[0]
        u = polygonal(unary_union(polys))
        if isinstance(u, MultiPolygon):
            areas = sorted(u.geoms, key=lambda p: p.area, reverse=True)
            outer = areas[0]
            holes = [p for p in areas[1:] if outer.contains(p)]
            if holes:
                outer = Polygon(outer.exterior, [h.exterior for h in holes])
            self.warnings.append("Profile 含多个独立多边形，取最大者作板框。")
            return outer
        return u

    def _parse_polygon(self, el) -> Optional[Polygon]:
        pts = []
        cur = None
        for ch in el:
            tag = lname(ch)
            if tag == "PolyBegin":
                cur = (fget(ch, "x", "X") * self.k, fget(ch, "y", "Y") * self.k)
                pts.append(cur)
            elif tag in ("PolyStepSegment", "PolyStep"):
                cur = (fget(ch, "x", "X", "endX") * self.k,
                       fget(ch, "y", "Y", "endY") * self.k)
                pts.append(cur)
            elif tag in ("PolyStepCurve", "PolyStepArc"):
                end = (fget(ch, "x", "X", "endX") * self.k,
                       fget(ch, "y", "Y", "endY") * self.k)
                cx = fget(ch, "centerX", "cx", default=math.nan)
                cy = fget(ch, "centerY", "cy", default=math.nan)
                center = (cx * self.k, cy * self.k) if not (
                    math.isnan(cx) or math.isnan(cy)) else None
                cw = str(get(ch, "clockwise", default="false")).lower() in ("true", "1", "yes")
                if cur is None or center is None:
                    pts.append(end)
                else:
                    pts.extend(_arc_points(cur, end, center, cw))
                cur = end
        pts = _dedupe_pts(pts)
        if len(pts) < 3:
            return None
        try:
            p = Polygon(pts)
            if not p.is_valid:
                p = make_valid(p)
            if isinstance(p, (Polygon, MultiPolygon)):
                return polygonal(p)
        except Exception:
            return None
        return None

    def _parse_contour(self, el) -> Optional[Polygon]:
        shell = None
        holes = []
        for ch in el:
            tag = lname(ch)
            if tag == "Polygon":
                p = self._parse_polygon(ch)
                if p is not None:
                    if shell is None:
                        shell = p
                    elif p.area < shell.area:
                        holes.append(p)
                    else:
                        holes.append(shell)
                        shell = p
            elif tag == "Cutout":
                for p in ch.iter():
                    if lname(p) == "Polygon":
                        pp = self._parse_polygon(p)
                        if pp is not None:
                            holes.append(pp)
        if shell is None:
            return None
        if holes:
            try:
                shell = Polygon(shell.exterior,
                                [h.exterior for h in holes] + list(shell.interiors))
            except Exception:
                pass
        return polygonal(shell)

    def _fallback_outline(self):
        geoms = [g for g in self.copper.values() if g is not None]
        if not geoms:
            return None
        u = polygonal(unary_union(geoms))
        if u is None:
            return None
        minx, miny, maxx, maxy = u.bounds
        return box(minx - 1.0, miny - 1.0, maxx + 1.0, maxy + 1.0)

    # ---- 铜层 -------------------------------------------------------------
    def _parse_copper(self, root):
        steps = [el for el in root.iter() if lname(el) == "Step"]
        if not steps:
            self.warnings.append("XML 中没有 Step 元素。")
            return
        # 选铜特征最多的 Step
        best, best_n = None, -1
        for st in steps:
            n = sum(1 for el in st.iter() if lname(el) == "LayerFeature")
            if n > best_n:
                best, best_n = st, n
        self._steps = [best]
        if len(steps) > 1:
            self.warnings.append(f"发现 {len(steps)} 个 Step，使用铜特征最多的一个。")

        # 先登记 LayerFeature 用到的层名（辅助判断铜层）
        for el in best.iter():
            if lname(el) == "LayerFeature":
                ref = get(el, "layerRef", "layer", "layerName")
                if ref:
                    self._layer_refs.add(ref)

        # 无 layerFunction 时，把出现在 LayerFeature 的层视为铜层
        for name, li in self.layer_by_name.items():
            if not li.func and name in self._layer_refs:
                li.copper = True
        for ref in self._layer_refs:
            if ref not in self.layer_by_name:
                # 未声明的层，按铜层处理
                self.layer_by_name[ref] = LayerInfo(name=ref, copper=True)
        self.copper_names = {n for n, li in self.layer_by_name.items() if li.copper}

        acc_pos = defaultdict(list)
        acc_neg = defaultdict(list)

        for lf in best.iter():
            if lname(lf) != "LayerFeature":
                continue
            ref = get(lf, "layerRef", "layer", "layerName")
            if not ref:
                continue
            li = self.layer_by_name.get(ref)
            # 非铜层（阻焊/丝印/钻孔层）跳过，但孔要收集
            is_copper = bool(li and li.copper)
            if not is_copper:
                self._scan_drills(lf)
                continue
            positive, negative = self._collect_layer_features(lf)
            if positive:
                acc_pos[ref].extend(positive)
            if negative:
                acc_neg[ref].extend(negative)
            self._scan_drills(lf)

        for ref in set(list(acc_pos) + list(acc_neg)):
            pos = self._merge(acc_pos[ref])
            neg = self._merge(acc_neg[ref])
            if pos is not None and neg is not None:
                try:
                    pos = polygonal(pos.difference(neg))
                except Exception:
                    pass
            if pos is not None and self.min_area > 0:
                pos = _filter_area(pos, self.min_area)
            if pos is not None and self.simplify > 0:
                try:
                    pos = polygonal(pos.simplify(self.simplify, preserve_topology=True))
                except Exception:
                    pass
            self.copper[ref] = pos
            self.feature_counts[ref] = len(list(iter_polygons(pos)))

    def _merge(self, geoms):
        geoms = [g for g in geoms if g is not None and not g.is_empty]
        if not geoms:
            return None
        try:
            u = unary_union(geoms)
        except Exception:
            u = None
        return polygonal(u)

    def _collect_layer_features(self, lf):
        pos, neg = [], []
        layer_ref = get(lf, "layerRef", "layer", "layerName", default="") or ""

        def walk(el, polarity="POSITIVE"):
            for ch in el:
                tag = lname(ch)
                self.tag_counts[tag] += 1
                if tag in ("Set", "Features", "Feature", "ColorGroup"):
                    pol = (get(ch, "polarity", default=polarity) or polarity).upper()
                    walk(ch, pol)
                elif tag == "PadStack":
                    walk(ch, polarity)
                elif tag in ("Pad", "LayerPad"):
                    g = self._pad_geom(ch, layer_ref)
                    if g is None:
                        continue
                    pol = polarity
                    usage = (get(ch, "padUse", "padUsage", default="") or "").upper()
                    if usage in ("ANTIPAD", "NEGATIVE"):
                        pol = "NEGATIVE"
                    (neg if pol.startswith("NEG") else pos).append(g)
                    self.feature_counts["pad"] += 1
                elif tag in ("UserPrimitiveRef", "UserPrimitive"):
                    pid = get(ch, "id", "standardPrimitiveId", default="") or ""
                    g = self.prims.get(pid) if pid else None
                    if g is not None:
                        x = fget(ch, "x", "X") * self.k
                        y = fget(ch, "y", "Y") * self.k
                        if abs(x) > 1e-12 or abs(y) > 1e-12:
                            g = affinity.translate(g, x, y)
                        (neg if polarity.startswith("NEG") else pos).append(g)
                        self.feature_counts["user_primitive"] += 1
                    else:
                        self.unknown_tags["user_primitive_unresolved"] += 1
                elif tag == "Line":
                    g = self._line_geom(ch)
                    if g is not None:
                        (neg if polarity.startswith("NEG") else pos).append(g)
                        self.feature_counts["line"] += 1
                elif tag == "Polyline":
                    g = self._polyline_geom(ch)
                    if g is not None:
                        (neg if polarity.startswith("NEG") else pos).append(g)
                        self.feature_counts["polyline"] += 1
                elif tag == "Contour":
                    g = self._parse_contour(ch)
                    if g is not None:
                        (neg if polarity.startswith("NEG") else pos).append(g)
                        self.feature_counts["contour"] += 1
                elif tag == "Polygon":
                    # 已被 Contour/Profile 处理；这里只兜底直接出现的 Polygon
                    g = self._parse_polygon(ch)
                    if g is not None:
                        (neg if polarity.startswith("NEG") else pos).append(g)
                        self.feature_counts["polygon"] += 1
                elif tag == "Rect":
                    g = self._rect_feature(ch)
                    if g is not None:
                        (neg if polarity.startswith("NEG") else pos).append(g)
                        self.feature_counts["rect"] += 1
                elif tag == "Circle":
                    g = circle(fget(ch, "x", "X") * self.k,
                               fget(ch, "y", "Y") * self.k,
                               fget(ch, "diameter", "Diameter") * self.k)
                    if g is not None:
                        (neg if polarity.startswith("NEG") else pos).append(g)
                        self.feature_counts["circle"] += 1
                elif tag in ("Donut", "Thermal", "Obround", "Diamond", "Octagon", "Triangle"):
                    g = self._build_primitive(ch)
                    if g is None:
                        continue
                    x = fget(ch, "x", "X", "centerX") * self.k
                    y = fget(ch, "y", "Y", "centerY") * self.k
                    if abs(x) > 1e-12 or abs(y) > 1e-12:
                        g = affinity.translate(g, x, y)
                    (neg if polarity.startswith("NEG") else pos).append(g)
                elif tag == "Arc":
                    g = self._arc_feature(ch)
                    if g is not None:
                        (neg if polarity.startswith("NEG") else pos).append(g)
                elif tag in ("Hole", "Slot", "SlotCavity"):
                    continue      # 孔单独收集
                elif tag == "Fiducial":
                    continue
                elif tag == "Text":
                    self.feature_counts["text_skipped"] += 1
                elif tag == "PinRef":
                    self.feature_counts["pinref_no_geometry"] += 1
                elif tag == "StandardPrimitiveRef":
                    continue
                elif len(ch):
                    walk(ch, polarity)
                else:
                    self.unknown_tags[tag] += 1

        walk(lf)
        if self.feature_counts.get("text_skipped"):
            self.warnings.append("含 Text 特征，已跳过（不影响铜层几何）。")
        if self.feature_counts.get("pinref_no_geometry"):
            self.warnings.append("部分 PinRef 未直接给出几何，依赖 LayerFeature 中的 Pad。")
        return pos, neg

    def _pad_geom(self, el, layer_ref: str = "") -> Optional[Polygon]:
        # 位置
        loc = first_child(el, "Location", "location")
        if loc is not None:
            x = fget(loc, "x", "X") * self.k
            y = fget(loc, "y", "Y") * self.k
        else:
            x = fget(el, "x", "X", "centerX") * self.k
            y = fget(el, "y", "Y", "centerY") * self.k
        rot = fget(el, "rotation", default=0.0)
        mirror = str(get(el, "mirror", default="false")).lower() in ("true", "1", "yes")
        # Xform 记录元件旋转/镜像，焊盘图形必须跟着转
        xf = first_child(el, "Xform", "xform")
        if xf is not None:
            rot += fget(xf, "rotation", default=0.0)
            if str(get(xf, "mirror", default="false")).lower() in ("true", "1", "yes"):
                mirror = not mirror

        # 直接内联图元
        shape = None
        for ch in el:
            if lname(ch) in PRIMITIVE_TAGS:
                shape = ch
                break
        if shape is not None:
            g = self._build_primitive(shape)
        else:
            pid = get(el, "standardPrimitiveRef", "standardPrimitiveId", "primitiveRef")
            psref = get(el, "padstackDefRef", "padStackDefRef")
            if not pid:
                for ch in el:
                    if lname(ch) in ("StandardPrimitiveRef", "StandardPrimitive",
                                     "UserPrimitiveRef", "UserPrimitive"):
                        pid = get(ch, "id", "standardPrimitiveId")
                        if pid:
                            break
                    if lname(ch) == "PadStackDefRef":
                        psref = get(ch, "name", "id")
            if pid:
                g = self.prims.get(pid)
            elif psref and psref in self.padstacks:
                # 按层名 + REGULAR 优先级选盘；ANTIPAD/THERMAL 只在没有 REGULAR 时兜底
                entries = self.padstacks[psref]
                g = None
                lr = layer_ref.upper()
                ordered = sorted(entries, key=lambda e: (
                    0 if (lr and e[0] == lr) else 1,
                    0 if e[5] == "REGULAR" else (1 if e[5] == "THERMAL" else 2),
                ))
                for layer, p, dx, dy, r, use in ordered:
                    if use == "ANTIPAD":
                        continue
                    if p:
                        gg = self.prims.get(p)
                        if gg is not None:
                            g = affinity.translate(gg, dx, dy)
                            rot += r
                            break
            else:
                g = None
        if g is None:
            self.unknown_tags["pad_without_primitive"] += 1
            return None

        key = (id(g), round(rot, 4), mirror)
        cached = self.rot_cache.get(key)
        if cached is None:
            gg = affinity.rotate(g, rot, origin=(0, 0))
            if mirror:
                gg = affinity.scale(gg, yfact=-1.0, origin=(0, 0))
            cached = gg
            self.rot_cache[key] = cached
        return affinity.translate(cached, x, y)

    def _line_geom(self, el) -> Optional[Polygon]:
        x1 = fget(el, "startX", "x1", "X1") * self.k
        y1 = fget(el, "startY", "y1", "Y1") * self.k
        x2 = fget(el, "endX", "x2", "X2") * self.k
        y2 = fget(el, "endY", "y2", "Y2") * self.k
        w = fget(el, "width", "lineWidth", default=0.0) * self.k
        if w <= 0:
            ld = first_child(el, "LineDesc")
            if ld is not None:
                w = fget(ld, "lineWidth", "width") * self.k
                cap = get(ld, "lineEnd", default="ROUND")
            else:
                cap = "ROUND"
        else:
            cap = "ROUND"
        if w <= 0:
            return None
        return stroke([(x1, y1), (x2, y2)], w, cap)

    def _polyline_points(self, el) -> list:
        pts = []
        cur = None
        for ch in el:
            tag = lname(ch)
            if tag == "PolyBegin":
                cur = (fget(ch, "x", "X") * self.k, fget(ch, "y", "Y") * self.k)
                pts.append(cur)
            elif tag == "PolyStepSegment":
                cur = (fget(ch, "x", "X", "endX") * self.k,
                       fget(ch, "y", "Y", "endY") * self.k)
                pts.append(cur)
            elif tag in ("PolyStepCurve", "PolyStepArc"):
                end = (fget(ch, "x", "X", "endX") * self.k,
                       fget(ch, "y", "Y", "endY") * self.k)
                cx = fget(ch, "centerX", "cx", default=math.nan)
                cy = fget(ch, "centerY", "cy", default=math.nan)
                center = None if (math.isnan(cx) or math.isnan(cy)) else (cx * self.k, cy * self.k)
                cw = str(get(ch, "clockwise", default="false")).lower() in ("true", "1", "yes")
                if cur is not None and center is not None:
                    pts.extend(_arc_points(cur, end, center, cw))
                else:
                    pts.append(end)
                cur = end
        return _dedupe_pts(pts)

    def _polyline_geom(self, el) -> Optional[Polygon]:
        ld = first_child(el, "LineDesc")
        w = fget(el, "width", default=0.0) * self.k
        cap = "ROUND"
        if ld is not None:
            if w <= 0:
                w = fget(ld, "lineWidth", "width") * self.k
            cap = get(ld, "lineEnd", default="ROUND")
        if w <= 0:
            return None
        pts = self._polyline_points(el)
        if len(pts) < 2:
            return None
        return stroke(pts, w, cap)

    def _arc_feature(self, el) -> Optional[Polygon]:
        x1 = fget(el, "startX", "x1") * self.k
        y1 = fget(el, "startY", "y1") * self.k
        x2 = fget(el, "endX", "x2") * self.k
        y2 = fget(el, "endY", "y2") * self.k
        cx = fget(el, "centerX", default=math.nan)
        cy = fget(el, "centerY", default=math.nan)
        w = fget(el, "width", default=0.0) * self.k
        if w <= 0:
            return None
        cw = str(get(el, "clockwise", default="false")).lower() in ("true", "1", "yes")
        if math.isnan(cx) or math.isnan(cy):
            return stroke([(x1, y1), (x2, y2)], w)
        pts = [(x1, y1)] + _arc_points((x1, y1), (x2, y2), (cx * self.k, cy * self.k), cw)
        return stroke(pts, w)

    def _rect_feature(self, el) -> Optional[Polygon]:
        x = fget(el, "x", "X", "centerX") * self.k
        y = fget(el, "y", "Y", "centerY") * self.k
        w = fget(el, "width", "Width") * self.k
        h = fget(el, "height", "Height") * self.k
        rot = fget(el, "rotation", default=0.0)
        return rect(x, y, w, h, rot)

    # ---- 图元定义 ---------------------------------------------------------
    def _build_primitive(self, el) -> Optional[Polygon]:
        tag = lname(el)
        x = fget(el, "x", "X", "centerX") * self.k
        y = fget(el, "y", "Y", "centerY") * self.k
        rot = fget(el, "rotation", default=0.0)

        if tag == "Circle":
            d = fget(el, "diameter", "Diameter") * self.k
            g = circle(0, 0, d)
        elif tag in ("Rect", "RectCenter"):
            # RectCenter 无 x/y，矩形中心固定在原点
            g = rect(0, 0, fget(el, "width", "Width") * self.k,
                     fget(el, "height", "Height") * self.k)
        elif tag == "Obround":
            g = obround(0, 0, fget(el, "width", "Width") * self.k,
                        fget(el, "height", "Height") * self.k)
        elif tag == "Line":
            x1 = fget(el, "startX", "x1") * self.k
            y1 = fget(el, "startY", "y1") * self.k
            x2 = fget(el, "endX", "x2") * self.k
            y2 = fget(el, "endY", "y2") * self.k
            w = fget(el, "width", "lineWidth") * self.k
            g = stroke([(x1, y1), (x2, y2)], w)
        elif tag in ("Polygon", "Contour"):
            g = self._parse_contour(el) if tag == "Contour" else self._parse_polygon(el)
        elif tag == "Arc":
            g = self._arc_feature(el)
        elif tag == "Donut":
            shape = (get(el, "shape", default="ROUND") or "ROUND").upper()
            od = fget(el, "outerDiameter", "OuterDiameter") * self.k
            idia = fget(el, "innerDiameter", "InnerDiameter") * self.k
            if shape == "ROUND":
                outer = circle(0, 0, od)
                inner = circle(0, 0, idia)
            else:
                w = fget(el, "outerWidth", "width") * self.k
                h = fget(el, "outerHeight", "height") * self.k
                iw = fget(el, "innerWidth") * self.k
                ih = fget(el, "innerHeight") * self.k
                outer = rect(0, 0, w, h)
                inner = rect(0, 0, iw, ih)
            g = outer.difference(inner) if outer is not None and inner is not None else outer
        elif tag == "Thermal":
            od = fget(el, "outerDiameter", "diameter") * self.k
            idia = fget(el, "innerDiameter") * self.k
            g = self._thermal(od, idia, fget(el, "spokeWidth") * self.k)
        elif tag == "Diamond":
            w = fget(el, "width") * self.k
            h = fget(el, "height") * self.k
            g = Polygon([(0, h / 2), (w / 2, 0), (0, -h / 2), (-w / 2, 0)]) if w > 0 and h > 0 else None
        elif tag == "Octagon":
            d = fget(el, "diameter", "width") * self.k
            if d > 0:
                r = d / 2
                pts = [(r * math.cos(math.pi / 8 + i * math.pi / 4),
                        r * math.sin(math.pi / 8 + i * math.pi / 4)) for i in range(8)]
                g = Polygon(pts)
            else:
                g = None
        elif tag == "Triangle":
            w = fget(el, "width") * self.k
            h = fget(el, "height") * self.k
            g = Polygon([(-w / 2, -h / 2), (w / 2, -h / 2), (0, h / 2)]) if w > 0 and h > 0 else None
        else:
            self.unknown_tags[tag] += 1
            return None

        if g is None:
            return None
        if rot:
            g = affinity.rotate(g, rot, origin=(0, 0))
        if abs(x) > 1e-12 or abs(y) > 1e-12:
            g = affinity.translate(g, x, y)
        return g

    def _thermal(self, od, idia, spoke_w=0.0):
        if od <= 0:
            return None
        outer = circle(0, 0, od)
        inner = circle(0, 0, idia) if idia > 0 else None
        if outer is None:
            return None
        g = outer.difference(inner) if inner is not None else outer
        if spoke_w <= 0:
            spoke_w = max(od * 0.15, 0.1)
        # 四个方向开缺口（近似 thermal relief）
        for ang in (0, 90):
            bar = rect(0, 0, od * 1.2, spoke_w, rot=ang)
            if bar is not None:
                g = g.difference(bar)
        return g

    # ---- 钻孔 -------------------------------------------------------------
    def _scan_drills(self, lf):
        for el in lf.iter():
            tag = lname(el)
            if tag == "Hole":
                loc = first_child(el, "Location", "location")
                if loc is not None:
                    x = fget(loc, "x", "X") * self.k
                    y = fget(loc, "y", "Y") * self.k
                else:
                    x = fget(el, "x", "X") * self.k
                    y = fget(el, "y", "Y") * self.k
                d = fget(el, "diameter", "Diameter") * self.k
                plated = "NP" not in (get(el, "platingStatus", default="") or "").upper()
                if d > 0:
                    self.drills.append((x, y, d, plated))
            elif tag in ("SlotCavity", "Slot"):
                loc = first_child(el, "Location", "location")
                if loc is None:
                    continue
                x = fget(loc, "x", "X") * self.k
                y = fget(loc, "y", "Y") * self.k
                shape = first_child(el, "Circle", "Rect")
                if shape is None:
                    continue
                if lname(shape) == "Circle":
                    d = fget(shape, "diameter") * self.k
                else:
                    d = max(fget(shape, "width") * self.k, fget(shape, "height") * self.k)
                if d > 0:
                    self.drills.append((x, y, d, False))

    @staticmethod
    def _dedupe_holes(holes):
        seen = {}
        for x, y, d, p in holes:
            key = (round(x, 3), round(y, 3), round(d, 3))
            if key not in seen:
                seen[key] = (x, y, d, p)
        return list(seen.values())

    # ---- 叠层 -------------------------------------------------------------
    def _build_bands(self, root) -> list:
        stackups = [el for el in root.iter() if lname(el) == "Stackup"]
        chosen = None
        for s in stackups:
            if any(lname(x) == "StackupLayer" for x in s.iter()):
                chosen = s
                break

        copper_names = [n for n, li in sorted(self.layer_by_name.items(),
                                              key=lambda kv: kv[1].order) if li.copper]
        entries = []
        overall = None
        if chosen is not None:
            overall = fget(chosen, "overallThickness", default=math.nan)
            if math.isnan(overall):
                overall = None
            else:
                overall *= self.k
            seq_present = True
            for el in chosen.iter():
                if lname(el) != "StackupLayer":
                    continue
                ref = get(el, "layerOrGroupRef", "layerRef", "layerGroupRef", default="") or ""
                th = fget(el, "thickness", default=math.nan)
                th = None if math.isnan(th) else th * self.k
                if th is None:
                    # 有些导出器把厚度放在父级 StackupGroup
                    par = el.getparent()
                    if par is not None and lname(par) == "StackupGroup":
                        gth = fget(par, "thickness", default=math.nan)
                        if not math.isnan(gth) and gth > 0:
                            th = gth * self.k
                seq = fget(el, "sequence", default=math.nan)
                seq = None if math.isnan(seq) else seq
                if seq is None:
                    seq_present = False
                mat = get(el, "materialType", default="") or ""
                if not mat:
                    for c in el.iter():
                        if lname(c) == "SpecRef":
                            mat = get(c, "name", "spec", "id", default="") or ""
                            if mat:
                                break
                entries.append({"ref": ref, "th": th, "seq": seq, "mat": mat})
            if seq_present:
                entries.sort(key=lambda e: e["seq"])

        refs = {e["ref"] for e in entries}
        copper_set = set(copper_names)

        # 叠层缺失、与铜层不匹配、或只有铜层没有介质 -> 合成
        has_dielectric = any(e["ref"] not in copper_set for e in entries)
        use_stack = bool(entries) and copper_set.issubset(refs) and has_dielectric
        if entries and not use_stack:
            missing = copper_set - refs
            if missing:
                self.warnings.append(
                    "叠层未包含铜层 " + ", ".join(sorted(missing)) + "，改用合成叠层。")
            elif not has_dielectric:
                self.warnings.append("叠层中没有介质层，改用合成叠层。")
            use_stack = False

        if not use_stack:
            return self._synthetic_bands(copper_names, overall)

        # 判断顺序：第一项若明确是 BOTTOM 且最后是 TOP，则反转
        def side_of(ref):
            li = self.layer_by_name.get(ref)
            return (li.side if li else "") or ""

        if len(entries) >= 2:
            s0, s1 = side_of(entries[0]["ref"]), side_of(entries[-1]["ref"])
            if s0 == "BOTTOM" and s1 == "TOP":
                entries.reverse()

        # 缺厚度补齐：0.0 是显式零厚度（如表面索引层），不能当缺失
        known = sum(e["th"] for e in entries if e["th"] is not None)
        missing = [e for e in entries if e["th"] is None]
        if missing:
            if overall and overall > known:
                fill = (overall - known) / len(missing)
            else:
                fill = 0.0
            for e in missing:
                e["th"] = fill
            if fill > 0:
                self.warnings.append("叠层中部分层缺厚度，已按剩余板厚均分。")
            else:
                self.warnings.append("叠层中部分层缺厚度且整体厚度不足，已按 0 处理。")

        total = sum(e["th"] for e in entries)
        if overall and total > 0 and abs(total - overall) / overall > 0.02:
            self.warnings.append(
                f"叠层厚度合计 {total:.3f} mm 与 overallThickness {overall:.3f} mm 不一致。")

        # 保证铜层都出现，且相邻铜层之间无介质时插入
        bands_top_down = []
        for e in entries:
            ref = e["ref"]
            if e["th"] is not None and e["th"] <= 1e-9:
                continue          # 零厚度表层索引，跳过
            is_cu = ref in copper_set or (self.layer_by_name.get(ref) and
                                          self.layer_by_name[ref].copper)
            if is_cu:
                li = self.layer_by_name.get(ref)
                bands_top_down.append(Band("copper", ref, e["mat"], e["th"], ref))
            else:
                bands_top_down.append(Band("dielectric", ref or "DIELECTRIC", e["mat"], e["th"], ref))

        # 相邻铜层直接相接时补一层极薄介质，避免实体直接重合
        fixed = []
        for b in bands_top_down:
            if fixed and fixed[-1].is_copper and b.is_copper:
                self.warnings.append("叠层中有相邻铜层，已插入 0.01 mm 介质。")
                fixed.append(Band("dielectric", "PREPREG_GAP", "FR4", 0.01))
            fixed.append(b)
        bands_top_down = fixed

        # 转为 bottom -> top 并赋 z
        bands = list(reversed(bands_top_down))
        z = 0.0
        for b in bands:
            b.z0 = z
            b.z1 = z + b.thickness
            z = b.z1
        return bands

    def _synthetic_bands(self, copper_names, overall):
        if overall is None:
            overall = 1.6
            self.warnings.append(f"未找到叠层，按 {overall:.2f} mm 默认板厚生成。")
        n = len(copper_names)
        if n == 0:
            b = Band("dielectric", "BOARD", "FR4", overall)
            b.z0, b.z1 = 0.0, overall
            return [b]
        # 文档顺序通常是从上到下
        top_down = list(reversed(copper_names))
        th_cu = [0.035 if (i == 0 or i == n - 1) else 0.0175 for i in range(n)]
        cu_total = sum(th_cu)
        remain = max(overall - cu_total, 0.02)
        diel = remain / max(1, n - 1) if n > 1 else 0.0

        bands = []
        z = 0.0
        for i, name in enumerate(top_down):
            b = Band("copper", name, "", th_cu[i], name)
            b.z0, b.z1 = z, z + th_cu[i]
            z = b.z1
            bands.append(b)
            if i < n - 1 and diel > 0:
                d = Band("dielectric", f"DIELECTRIC_{i+1}", "FR4", diel)
                d.z0, d.z1 = z, z + diel
                z = d.z1
                bands.append(d)
        if abs(z - overall) > 0.02:
            self.warnings.append(f"合成叠层总厚 {z:.3f} mm（目标 {overall:.3f} mm）。")
        return bands


def _dedupe_pts(pts):
    out = []
    for p in pts:
        if not out or abs(p[0] - out[-1][0]) > 1e-9 or abs(p[1] - out[-1][1]) > 1e-9:
            out.append(p)
    if len(out) > 1 and abs(out[0][0] - out[-1][0]) < 1e-9 and abs(out[0][1] - out[-1][1]) < 1e-9:
        out.pop()
    return out


def _filter_area(g, min_area):
    polys = [p for p in iter_polygons(g) if p.area >= min_area]
    if not polys:
        return None
    u = unary_union(polys)
    return polygonal(u)


# ---------------------------------------------------------------------------
# IDF (EMN/EMP) 解析 —— 只有板框/孔/器件，没有铜箔
# ---------------------------------------------------------------------------
class IdfParser:
    def __init__(self, emn: str, emp: Optional[str] = None,
                 force_units: Optional[str] = None, thickness: Optional[float] = None):
        self.emn = emn
        self.emp = emp
        self.force_units = force_units
        self.thickness = thickness
        self.k = 25.4
        self.warnings = []

    def parse(self) -> BoardModel:
        text = _read_text(self.emn)
        lines = text.splitlines()
        units_tok = None
        for ln in lines[:40]:
            t = ln.split()
            if t and t[0].upper() == "BOARD_FILE":
                if t:
                    units_tok = t[-1]
                break

        if self.force_units:
            self.k = unit_factor(self.force_units)
        elif units_tok in ("1",):
            self.k = 25.4
        elif units_tok in ("2",):
            self.k = 1.0
        else:
            self.k = 25.4
            self.warnings.append("IDF 单位未识别，按 inch 处理；可用 --units 指定。")

        sections = _split_idf_sections(lines)
        pts = []
        thickness = None
        for ln in sections.get("BOARD_OUTLINE", []):
            t = ln.split()
            if not t:
                continue
            if len(t) == 1 and _is_number(t[0]):
                thickness = abs(float(t[0])) * self.k
                continue
            if t[0] in ("0", "1") and len(t) >= 3:
                try:
                    pts.append((float(t[1]) * self.k, float(t[2]) * self.k))
                except ValueError:
                    continue
        pts = _dedupe_pts(pts)
        outline = None
        if len(pts) >= 3:
            try:
                outline = Polygon(pts)
                if not outline.is_valid:
                    outline = make_valid(outline)
                outline = polygonal(outline)
            except Exception:
                outline = None
        if outline is None:
            self.warnings.append("EMN 中未解析到板框。")

        holes = []
        for ln in sections.get("DRILLED_HOLES", []):
            t = ln.split()
            if len(t) < 4:
                continue
            try:
                d = float(t[0]) * self.k
                x = float(t[1]) * self.k
                y = float(t[2]) * self.k
                plated = any(x.upper().startswith("PTH") and not x.upper().startswith("NPTH")
                             for x in t[3:])
                holes.append((x, y, d, plated))
            except ValueError:
                continue

        comps = []
        place = sections.get("PLACEMENT", [])
        i = 0
        while i < len(place) - 1:
            l1, l2 = place[i].split(), place[i + 1].split()
            if l2 and l2[-1].upper() in ("PLACED", "UNPLACED") and len(l2) >= 5:
                try:
                    x = float(l2[0]) * self.k
                    y = float(l2[1]) * self.k
                    rot = float(l2[-3])
                    side = l2[-2].upper()
                    comps.append({
                        "ref": l1[-1], "package": l1[0],
                        "x": x, "y": y, "rotation": rot, "side": side,
                    })
                    i += 2
                    continue
                except (ValueError, IndexError):
                    pass
            i += 1

        if thickness is None:
            thickness = self.thickness or 1.6
            self.warnings.append(f"EMN 无板厚，使用 {thickness:.2f} mm。")
        elif self.thickness:
            thickness = self.thickness

        b = Band("dielectric", "BOARD", "FR4", thickness)
        b.z0, b.z1 = 0.0, thickness

        self.warnings.append(
            "EMN/EMP 是 IDF 中性文件，只含板框/钻孔/器件位置，不含铜箔走线与铜面；"
            "无法生成精确铜层。要铜层请用同一设计的 IPC-2581 XML。")

        return BoardModel(
            fmt="IDF(EMN/EMP)",
            source=str(self.emn),
            units="MILLIMETER" if self.k == 1 else "INCH",
            outline=outline,
            holes=holes,
            layers=[],
            copper={},
            bands=[b],
            components=comps,
            warnings=self.warnings,
            stats={"placements": len(comps)},
        )


def _read_text(path):
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def _is_number(s):
    try:
        float(s)
        return True
    except ValueError:
        return False


def _split_idf_sections(lines):
    sections = defaultdict(list)
    cur = None
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if s.startswith("."):
            t = s.split()
            key = t[0][1:].upper()
            if key.startswith("END_"):
                cur = None
            else:
                cur = key
                sections.setdefault(cur, [])
        elif cur:
            sections[cur].append(s)
    return sections


# ---------------------------------------------------------------------------
# 3D 构建
# ---------------------------------------------------------------------------
def _ocp_imports():
    """延迟导入 OCP，inspect 模式无需加载 CAD 内核。"""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepBuilderAPI import (
        BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon,
        BRepBuilderAPI_MakeWire, BRepBuilderAPI_Transform,
    )
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakePrism
    from OCP.BRep import BRep_Builder
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.gp import gp_Ax2, gp_Circ, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.STEPControl import STEPControl_StepModelType, STEPControl_Writer
    from OCP.StlAPI import StlAPI_Writer
    from OCP.TopoDS import TopoDS, TopoDS_Compound
    return dict(
        BRepAlgoAPI_Cut=BRepAlgoAPI_Cut,
        BRepBuilderAPI_MakeEdge=BRepBuilderAPI_MakeEdge,
        BRepBuilderAPI_MakeFace=BRepBuilderAPI_MakeFace,
        BRepBuilderAPI_MakePolygon=BRepBuilderAPI_MakePolygon,
        BRepBuilderAPI_MakeWire=BRepBuilderAPI_MakeWire,
        BRepBuilderAPI_Transform=BRepBuilderAPI_Transform,
        BRepPrimAPI_MakeCylinder=BRepPrimAPI_MakeCylinder,
        BRepPrimAPI_MakePrism=BRepPrimAPI_MakePrism,
        BRep_Builder=BRep_Builder,
        BRepMesh_IncrementalMesh=BRepMesh_IncrementalMesh,
        gp_Ax2=gp_Ax2, gp_Circ=gp_Circ, gp_Dir=gp_Dir, gp_Pnt=gp_Pnt,
        gp_Trsf=gp_Trsf, gp_Vec=gp_Vec,
        IFSelect_ReturnStatus=IFSelect_ReturnStatus,
        STEPControl_StepModelType=STEPControl_StepModelType,
        STEPControl_Writer=STEPControl_Writer,
        StlAPI_Writer=StlAPI_Writer,
        TopoDS=TopoDS, TopoDS_Compound=TopoDS_Compound,
    )


def _ocp_wire(ocp, pts, reverse=False):
    mp = ocp["BRepBuilderAPI_MakePolygon"]()
    for x, y in pts:
        mp.Add(ocp["gp_Pnt"](float(x), float(y), 0.0))
    mp.Close()
    w = ocp["TopoDS"].Wire_s(mp.Wire())
    if reverse:
        w = ocp["TopoDS"].Wire_s(w.Reversed())
    return w


def _ocp_circle_wire(ocp, x, y, r):
    """精确圆孔环，避免用 40 边形近似钻孔（STEP 体积可小一个数量级）。"""
    circ = ocp["gp_Circ"](
        ocp["gp_Ax2"](ocp["gp_Pnt"](float(x), float(y), 0.0),
                      ocp["gp_Dir"](0.0, 0.0, 1.0)), float(r))
    e = ocp["BRepBuilderAPI_MakeEdge"](circ).Edge()
    w = ocp["BRepBuilderAPI_MakeWire"](e).Wire()
    return ocp["TopoDS"].Wire_s(w.Reversed())


def _ocp_face(ocp, poly: Polygon, circle_holes=None):
    """shapely Polygon -> OCCT Face（外环 CCW，孔环 CW）。
    circle_holes: [(x, y, r), ...] 以真实圆弧加入的孔，用于钻孔。"""
    from shapely.geometry.polygon import orient
    try:
        p = orient(poly, 1.0)
    except Exception:
        p = poly
    ext = list(p.exterior.coords)[:-1]
    if len(ext) < 3:
        return None
    try:
        mf = ocp["BRepBuilderAPI_MakeFace"](_ocp_wire(ocp, ext))
        for ring in p.interiors:
            pts = list(ring.coords)[:-1]
            if len(pts) >= 3:
                # orient() 已把孔环设为 CW，与 OCC 的面方向相反，直接加入即为孔
                mf.Add(_ocp_wire(ocp, pts))
        for (x, y, r) in (circle_holes or []):
            if r > 0:
                mf.Add(_ocp_circle_wire(ocp, x, y, r))
        return mf.Face()
    except Exception:
        return None


def _ocp_translate(ocp, shape, z):
    if abs(z) < 1e-12:
        return shape
    tr = ocp["gp_Trsf"]()
    tr.SetTranslation(ocp["gp_Vec"](0.0, 0.0, float(z)))
    return ocp["BRepBuilderAPI_Transform"](shape, tr, True).Shape()


def _ocp_prism(ocp, face, z0, thickness):
    s = ocp["BRepPrimAPI_MakePrism"](face, ocp["gp_Vec"](0.0, 0.0, float(thickness))).Shape()
    return _ocp_translate(ocp, s, z0)


def _ocp_cylinder(ocp, x, y, d, z0, height):
    ax = ocp["gp_Ax2"](ocp["gp_Pnt"](float(x), float(y), float(z0)),
                       ocp["gp_Dir"](0.0, 0.0, 1.0))
    return ocp["BRepPrimAPI_MakeCylinder"](ax, float(d) / 2.0, float(height)).Shape()


def _ocp_compound(ocp, shapes):
    comp = ocp["TopoDS_Compound"]()
    b = ocp["BRep_Builder"]()
    b.MakeCompound(comp)
    n = 0
    for s in shapes:
        if s is None:
            continue
        try:
            if s.IsNull():
                continue
        except Exception:
            pass
        b.Add(comp, s)
        n += 1
    return comp if n else None


def build_3d(model: BoardModel, args, progress=None):
    ocp = _ocp_imports()
    warnings = model.warnings
    if model.outline is None:
        raise RuntimeError("没有板框，无法建模。")

    outline = model.outline
    if not isinstance(outline, Polygon):
        polys = list(iter_polygons(outline))
        if not polys:
            raise RuntimeError("板框为空。")
        outline = max(polys, key=lambda p: p.area)

    total = model.thickness

    copper_only = bool(getattr(args, "copper_only", False))
    detail = getattr(args, "detail", "copper")
    if copper_only and detail != "copper":
        raise RuntimeError("--copper-only 只能与 --detail copper 一起使用。")

    # ---- 钻孔：作为真实圆弧孔环加入介质面，再做拉伸 ----
    # 不要用 32/40 边形近似：每个孔会产生几十个侧立面，STEP 会膨胀几十倍。
    drill_circles = []
    drill_2d = None
    if model.holes and args.drills:
        n = len(model.holes)
        if n > args.max_drills and not args.force_drills:
            warnings.append(f"钻孔 {n} 个超过阈值 {args.max_drills}，已跳过钻孔裁剪；"
                            f"可用 --force-drills 强制。")
        else:
            for x, y, d, plated in model.holes:
                if d <= 0:
                    continue
                if not outline.buffer(1e-6).contains(Point(x, y)):
                    continue
                drill_circles.append((x, y, d / 2.0))
            if drill_circles:
                drill_2d = polygonal(unary_union([
                    Point(x, y).buffer(r, quad_segs=TES["circle"])
                    for (x, y, r) in drill_circles]))
                where = "铜层开孔" if copper_only else "铜层与介质均开孔"
                warnings.append(f"已扣除 {len(drill_circles)} 个钻孔（{where}）。")

    # 无铜区域的介质面；detail=board 时整块板都用它（仅铜层模式不需要）
    board_faces = []
    if not copper_only:
        for poly in iter_polygons(outline):
            f = _ocp_face(ocp, poly, circle_holes=drill_circles)
            if f is not None:
                board_faces.append(f)
        if not board_faces:
            raise RuntimeError("板框面构建失败。")

    solids = []
    per_band = []
    skipped = 0

    bands = [b for b in model.bands if b.z1 - b.z0 > 0]
    if copper_only:
        bands = [b for b in bands if b.is_copper]
        print("模式: 仅生成铜层（跳过介质层与 FR4 填充）")
    n_bands = len(bands)
    for bi, band in enumerate(bands, 1):
        t = band.z1 - band.z0
        if progress is not None:
            progress(bi - 1, n_bands, f"正在生成 {band.name} ({bi}/{n_bands})")
        copper_solids = []
        fill_solids = []

        if band.is_copper and detail != "board":
            geom_raw = model.copper.get(band.ref) or model.copper.get(band.name)
            if geom_raw is None:
                continue
            # 钻孔穿透铜层：焊盘由实心圆变成圆环，平面层得到真实钻孔。
            geom = geom_raw
            if drill_2d is not None:
                geom = polygonal(geom_raw.difference(drill_2d)) or geom_raw
            for poly in iter_polygons(geom):
                if poly.area < 1e-9:
                    continue
                f = _ocp_face(ocp, poly)
                if f is None:
                    skipped += 1
                    continue
                try:
                    copper_solids.append(_ocp_prism(ocp, f, band.z0, t))
                except Exception:
                    skipped += 1

            # 铜层带内的介质填充：板框 - 铜 - 钻孔 = 介质（FR4 在走线之间连续）
            if not args.no_fill and not copper_only:
                try:
                    fill = polygonal(outline.difference(geom_raw))
                    if fill is not None and drill_2d is not None:
                        fill = polygonal(fill.difference(drill_2d))
                except Exception:
                    fill = None
                if fill is not None:
                    for poly in iter_polygons(fill):
                        if poly.area < 1e-9:
                            continue
                        f = _ocp_face(ocp, poly)
                        if f is None:
                            continue
                        try:
                            fill_solids.append(_ocp_prism(ocp, f, band.z0, t))
                        except Exception:
                            continue
        else:
            # 介质层（--detail board 时铜层也按介质建），孔已在 2D 扣除
            for f in board_faces:
                try:
                    fill_solids.append(_ocp_prism(ocp, f, band.z0, t))
                except Exception as e:
                    warnings.append(f"介质层 {band.name} 拉伸失败: {e}")

        band_solids = copper_solids + fill_solids
        solids.extend(band_solids)
        # 拆分导出时把铜和介质分成两个文件，方便 Abaqus 逐件赋材料
        if copper_solids:
            per_band.append((f"{band.name}_CU", copper_solids))
        if fill_solids:
            suffix = "FR4" if band.is_copper else ""
            label = f"{band.name}_FR4" if suffix else band.name
            per_band.append((label, fill_solids))

    # ---- 电镀孔 -> 实心铜柱 ----
    if args.vias and model.holes:
        via_solids = []
        for x, y, d, plated in model.holes:
            if not plated or d <= 0:
                continue
            if not outline.buffer(1e-6).contains(Point(x, y)):
                continue
            try:
                via_solids.append(_ocp_cylinder(ocp, x, y, d, 0.0, total))
            except Exception:
                continue
        if via_solids:
            solids.extend(via_solids)
            warnings.append(f"已生成 {len(via_solids)} 个实心铜柱（--vias）。")

    if not solids:
        raise RuntimeError("没有生成任何实体，请检查 --detail / 输入数据。")
    if skipped:
        warnings.append(f"{skipped} 个铜岛建面失败已跳过。")

    compound = _ocp_compound(ocp, solids)
    return compound, per_band


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _silent_stdout():
    """临时把 C/C++ 层的 stdout 重定向到 devnull（OCCT 统计输出走 fd 1）。"""
    try:
        sys.stdout.flush()
        fd = os.dup(1)
        nul = os.open(os.devnull, os.O_WRONLY)
        os.dup2(nul, 1)
    except Exception:
        yield
        return
    try:
        yield
    finally:
        try:
            sys.stdout.flush()
            os.dup2(fd, 1)
            os.close(nul)
            os.close(fd)
        except Exception:
            pass


def _export_step(ocp, shape, path: Path):
    w = ocp["STEPControl_Writer"]()
    # OCCT 的 STEP writer 会往 stdout 打一大段统计，导出时临时静音
    with _silent_stdout():
        w.Transfer(shape, ocp["STEPControl_StepModelType"].STEPControl_AsIs)
        st = w.Write(str(path))
    if st != ocp["IFSelect_ReturnStatus"].IFSelect_RetDone:
        raise RuntimeError(f"STEP 写出失败: {path}")


def _export_stl(ocp, shape, path: Path, tol: float):
    # StlAPI_Writer 不会自动网格化，必须先离散
    ocp["BRepMesh_IncrementalMesh"](shape, float(tol), False, 0.15, True)
    w = ocp["StlAPI_Writer"]()
    w.ASCIIMode = False
    if not w.Write(shape, str(path)):
        raise RuntimeError(f"STL 写出失败: {path}")


def write_outputs(model, compound, per_band, args):
    ocp = _ocp_imports()

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    fmt = args.format or ("stl" if out.suffix.lower() == ".stl" else "step")
    t0 = time.time()

    if fmt == "stl":
        _export_stl(ocp, compound, out, args.tolerance)
    else:
        _export_step(ocp, compound, out)

    print(f"\n已写出: {out}  ({out.stat().st_size / 1e6:.2f} MB, {time.time()-t0:.1f}s)")

    if args.split:
        d = Path(args.split)
        d.mkdir(parents=True, exist_ok=True)
        idx = 0
        for label, bs in per_band:
            if not bs:
                continue
            idx += 1
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", label).strip("_") or f"band{idx}"
            p = d / f"{idx:02d}_{safe}.step"
            comp = _ocp_compound(ocp, bs)
            if comp is not None:
                _export_step(ocp, comp, p)
        print(f"已按层导出 {idx} 个 STEP 到: {d}")


def print_report(model: BoardModel, args):
    print("=" * 78)
    print(f"文件格式 : {model.fmt}")
    print(f"源文件   : {model.source}")
    print(f"单位     : {model.units}")
    if model.outline is not None:
        minx, miny, maxx, maxy = model.outline.bounds
        print(f"板框尺寸 : {maxx-minx:.3f} x {maxy-miny:.3f} mm  "
              f"(面积 {model.outline.area:.1f} mm2)")
    print(f"总厚度   : {model.thickness:.3f} mm")
    print(f"叠层数   : {len(model.bands)}  (铜层 {len(model.copper_layers)})")
    print(f"钻孔     : {len(model.holes)}")
    if model.components:
        print(f"器件     : {len(model.components)}")

    if model.bands:
        print("\n叠层（从下到上）:")
        print(f"{'#':>2} {'类型':<6} {'名称':<22} {'Z0':>9} {'Z1':>9} {'厚度':>8} {'材料':<12}")
        print("-" * 78)
        for i, b in enumerate(model.bands, 1):
            kind = "铜" if b.is_copper else "介质"
            print(f"{i:>2} {kind:<6} {b.name[:22]:<22} {b.z0:>9.4f} {b.z1:>9.4f} "
                  f"{b.thickness:>8.4f} {b.material[:12]:<12}")

    if model.copper:
        print("\n铜层统计:")
        ol_area = model.outline.area if model.outline is not None else 0.0
        for name, g in model.copper.items():
            if g is None:
                print(f"  {name:<24} 无几何")
                continue
            n = len(list(iter_polygons(g)))
            fill = (g.area / ol_area * 100.0) if ol_area else 0.0
            print(f"  {name:<24} 铜岛 {n:>6}  面积 {g.area:>10.1f} mm2  覆盖率 {fill:>5.1f}%")

    if model.stats.get("features"):
        print("\n特征计数:", json.dumps(model.stats["features"], ensure_ascii=False))
    if args.dump and model.stats.get("tag_counts"):
        print("\nXML 标签统计:")
        for k, v in list(model.stats["tag_counts"].items())[:40]:
            print(f"  {k:<24} {v}")
        if model.stats.get("unknown_tags"):
            print("未识别标签:", json.dumps(model.stats["unknown_tags"], ensure_ascii=False))

    if model.warnings:
        print("\n提示/警告:")
        seen = set()
        for w in model.warnings:
            if w in seen:
                continue
            seen.add(w)
            print(f"  ! {w}")
    print("=" * 78)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def detect_format(path: Path) -> str:
    head = path.read_bytes()[:16384]
    txt = head.decode("utf-8", errors="ignore")
    low = txt.lower()
    if "<ipc-2581" in low or (txt.lstrip().startswith("<?xml") and "<content" in low):
        return "ipc2581"
    if txt.lstrip().startswith("<?xml"):
        return "xml"
    if ".header" in low and (".board_outline" in low or ".placement" in low):
        return "idf"
    if "odbpp" in low or "matrix.xml" in low:
        return "odb"
    return "unknown"


def load_model(path_str: str, args) -> BoardModel:
    path = Path(path_str)
    if not path.exists():
        raise FileNotFoundError(path)

    ext = path.suffix.lower()
    if ext == ".emn":
        emp = None
        cand = path.with_suffix(".emp")
        if cand.exists():
            emp = str(cand)
        return IdfParser(str(path), emp, args.units, args.thickness).parse()

    fmt = detect_format(path)
    if fmt == "ipc2581":
        return Ipc2581Parser(str(path), args.units, args.simplify, args.min_area).parse()
    if fmt == "xml":
        # 可能是 IPC-2581 但根节点名不同，尝试按 IPC-2581 解析
        try:
            m = Ipc2581Parser(str(path), args.units, args.simplify, args.min_area).parse()
            if m.bands and (m.copper or m.outline):
                return m
        except Exception:
            pass
        raise RuntimeError(
            "XML 不是可识别的 IPC-2581 文件。请确认导出格式为 IPC-2581（XML），"
            "ODB++/Gerber 需要先转换。")
    if fmt == "idf":
        return IdfParser(str(path), None, args.units, args.thickness).parse()
    if fmt == "odb":
        raise RuntimeError(
            "检测到 ODB++（XML 只是索引文件）。ODB++ 目录需先转 IPC-2581，"
            "或用 ODB++ 解析器；当前脚本不支持 ODB++ 目录。")
    raise RuntimeError(f"无法识别文件格式: {path}")


def add_common(p):
    p.add_argument("input", help="IPC-2581 XML，或 IDF 的 .emn")
    p.add_argument("--units", choices=["mm", "inch", "mil", "MICRON", "MILLIMETER", "INCH"],
                   default=None, help="强制输入单位")
    p.add_argument("--thickness", type=float, default=None, help="板厚 mm（叠层缺失时）")
    p.add_argument("--simplify", type=float, default=0.0, help="铜多边形简化容差 mm")
    p.add_argument("--min-area", type=float, default=0.0, help="丢弃小于该面积的铜岛 mm2")
    p.add_argument("--quality", choices=list(QUALITY_PRESETS), default="high",
                   help="离散精度：ultra/high/medium/fast，直接影响 STEP 大小和导出时间")
    p.add_argument("--max-drills", type=int, default=2000, help="超过该孔数默认不裁孔")
    p.add_argument("--dump", action="store_true", help="打印 XML 标签统计（排错用）")
    p.add_argument("--debug", action="store_true")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="ECAD(IPC-2581 / IDF) -> 精确铜层 3D 实体 -> STEP/STL",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("inspect", help="只解析并报告，不建 3D")
    add_common(pi)

    pc = sub.add_parser("convert", help="生成 STEP/STL")
    add_common(pc)
    pc.add_argument("-o", "--output", required=True, help="输出 .step / .stl")
    pc.add_argument("--format", choices=["step", "stl"], default=None)
    pc.add_argument("--detail", choices=["copper", "board", "simple"], default="copper",
                    help="copper=全铜层(默认)，board=仅介质，simple=整板块")
    pc.add_argument("--copper-only", action="store_true",
                    help="只生成铜层，不生成介质层（默认铜层+介质都生成）")
    pc.add_argument("--split", default=None, metavar="DIR", help="每层单独导出 STEP 到目录")
    pc.add_argument("--drills", dest="drills", action="store_true", default=True)
    pc.add_argument("--no-drills", dest="drills", action="store_false")
    pc.add_argument("--force-drills", action="store_true")
    pc.add_argument("--no-fill", action="store_true",
                    help="铜层带内不填介质（会留空气隙，不推荐）")
    pc.add_argument("--vias", action="store_true", help="电镀孔做成实心铜柱")
    pc.add_argument("--tolerance", type=float, default=0.02, help="STL 弦高容差 mm")

    args = ap.parse_args(argv)
    set_quality(getattr(args, "quality", "high"))

    try:
        t0 = time.time()
        model = load_model(args.input, args)
        print_report(model, args)

        if args.cmd == "inspect":
            return 0

        if args.detail == "simple":
            model.bands = _simple_band(model)

        compound, per_band = build_3d(model, args)
        print(f"\n实体构建完成: {time.time()-t0:.1f}s")
        write_outputs(model, compound, per_band, args)

        print("\n导入 Abaqus 提示:")
        print("  1. File > Import > Part > STEP，单位选 mm，勾选 Combine Bodies"
              "（否则每个铜岛会变成一个独立 Part）。")
        if getattr(args, "copper_only", False):
            print("  2. 用 --split out_dir 逐层导出，每层一个铜层文件。")
        else:
            print("  2. 用 --split out_dir 逐层导出，便于逐层赋 FR4/铜材料。")
        print("  3. 相邻层共面，可 Search Contact Pairs + Tie，或 Mesh > Merge。")
        print("  4. 全铜模型网格量大时，先 --simplify 0.01 --min-area 0.01 简化。")
        return 0
    except Exception as e:
        if getattr(args, "debug", False):
            traceback.print_exc()
        print(f"\n[错误] {e}", file=sys.stderr)
        return 2


def _simple_band(model):
    t = model.thickness or 1.6
    b = Band("dielectric", "BOARD", "FR4", t)
    b.z0, b.z1 = 0.0, t
    return [b]


if __name__ == "__main__":
    raise SystemExit(main())
