# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_data_files

project = Path(SPECPATH)

# 只收集 OCP 的 Python 包装文件，不用 collect_all：
# collect_all 会把 OCP.IVtk 等隐藏导入也拉进来，从而把 277 MB 的 VTK 一起打包。
ocp_datas = collect_data_files("OCP")

# delvewheel 把 OCC 的 DLL 放在 site-packages/cadquery_ocp.libs，
# 不在 OCP 包内，必须逐个按原目录名打进包。
libs_dir = Path(sys.prefix) / "Lib" / "site-packages" / "cadquery_ocp.libs"
ocp_libs = [(str(p), "cadquery_ocp.libs") for p in sorted(libs_dir.glob("*.dll"))]

datas = [
    (str(project / "assets" / "ecad2step.png"), "assets"),
    (str(project / "assets" / "ecad2step.ico"), "assets"),
    (str(project / "samples" / "demo_board.xml"), "samples"),
    (str(project / "packaging" / "vtk.libs" / ".keep"), "vtk.libs"),
] + ocp_datas

binaries = ocp_libs

hiddenimports = [
    "OCP.OCP",
    "OCP.BRepAlgoAPI",
    "OCP.BRepBuilderAPI",
    "OCP.BRepPrimAPI",
    "OCP.BRep",
    "OCP.BRepMesh",
    "OCP.gp",
    "OCP.IFSelect",
    "OCP.STEPControl",
    "OCP.StlAPI",
    "OCP.TopoDS",
    "lxml.etree",
    "lxml._elementpath",
]

a = Analysis(
    ["ecad2step_gui.py"],
    pathex=[str(project)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["matplotlib", "scipy", "pandas", "PyQt5", "PySide2", "PySide6",
              "vtkmodules"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ECAD2STEP",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(project / "assets" / "ecad2step.ico"),
)
