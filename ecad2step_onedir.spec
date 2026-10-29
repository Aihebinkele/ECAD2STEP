# -*- mode: python ; coding: utf-8 -*-
"""One-folder 打包：启动快（无需每次解包 300MB），适合日常使用。"""
from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_data_files

project = Path(SPECPATH)
ocp_datas = collect_data_files("OCP")
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
    "OCP.OCP", "OCP.BRepAlgoAPI", "OCP.BRepBuilderAPI",
    "OCP.BRepPrimAPI", "OCP.BRep", "OCP.BRepMesh", "OCP.gp",
    "OCP.IFSelect", "OCP.STEPControl", "OCP.StlAPI", "OCP.TopoDS",
    "lxml.etree", "lxml._elementpath",
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
    excludes=["matplotlib", "scipy", "pandas", "PyQt5", "PySide2", "PySide6", "vtkmodules"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ECAD2STEP",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(project / "assets" / "ecad2step.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="ECAD2STEP",
)
