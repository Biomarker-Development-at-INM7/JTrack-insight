# PyInstaller spec for the local browser-launching desktop build.

from pathlib import Path


project_root = Path.cwd()
src_root = project_root / "src"


a = Analysis(
    [str(project_root / "main.py")],
    pathex=[str(project_root), str(src_root)],
    binaries=[],
    datas=[
        (str(project_root / "README.md"), "."),
        (str(project_root / "docs"), "docs"),
    ],
    hiddenimports=[
        "uvicorn",
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.lifespan.on",
        "fastapi",
        "plotly",
        "trackautism_app.desktop.launcher",
        "trackautism_app.server.web",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pyarrow",
        "duckdb",
        "pytest",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="JTrack Insight",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=True,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="JTrack Insight",
)

app = BUNDLE(
    coll,
    name="JTrack Insight.app",
    icon=str(project_root / "assets" / "jtrack-insight.icns") if (project_root / "assets" / "jtrack-insight.icns").exists() else None,
    bundle_identifier="de.fzj.jutrack.jtrackinsight",
)
