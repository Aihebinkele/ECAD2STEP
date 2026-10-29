# -*- coding: utf-8 -*-
"""Abaqus/CAE noGUI 校验：导入 STEP 并打印 cell 数、体积、包围盒。"""
from abaqus import *
from abaqusConstants import *
from caeModules import *
import os
import sys

env_paths = os.environ.get("ABAQUS_CHECK_STEP", "")
paths = [p for p in env_paths.split(";") if p.strip()]
if not paths:
    paths = [r"F:/FTP/projects/ecad2step/out/gui_demo.step"]

for i, path in enumerate(paths):
    name = "M%d" % i
    mdb.Model(name=name)
    model = mdb.models[name]
    try:
        # Abaqus 2017: STEP 必须先 openStep 得到 AcisFile 对象，不能直接传路径
        step_obj = mdb.openStep(path, scale=1.0)
        p = model.PartFromGeometryFile(
            name="P%d" % i, geometryFile=step_obj, combine=True,
            dimensionality=THREE_D, type=DEFORMABLE_BODY)
        cells = len(p.cells)
        vol = p.getVolume()
        extra = ""
        try:
            q = p.queryGeometry(printResults=False)
            b = q.get("boundingBox", None)
            if b is not None:
                extra = " bbox=%s" % (b,)
        except Exception:
            pass
        print("IMPORT_OK %s cells=%d volume=%.6f%s"
              % (path, cells, vol, extra))
    except Exception as e:
        print("IMPORT_FAIL %s %s" % (path, e))
print("ABAQUS_CHECK_DONE")
