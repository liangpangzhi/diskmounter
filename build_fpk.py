#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build diskmounter_1.3.0.fpk reproducibly.

Stages from the current 1.3.0.fpk (captures cmd/, config/, wizard/, ICONs,
manifest) and overlays the updated app/ from _unpack/app, then rebuilds
app.tgz and the outer fpk with a correct md5 checksum in manifest.
"""
import os, re, tarfile, hashlib, shutil, io

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC_FPK = os.path.join(ROOT, "diskmounter_1.3.0.fpk")
APP_SRC = os.path.join(ROOT, "_unpack", "app")
STAGE = os.path.join(ROOT, "_stage")
OUT_FPK = os.path.join(ROOT, "diskmounter_1.3.0.fpk")
MANIFEST_NAME = "manifest"

EXCLUDE_DIRS = {"__pycache__"}
EXCLUDE_FILES = set()

def rm_tree(p):
    if os.path.exists(p):
        shutil.rmtree(p)

def extract_all(fpk, dest):
    os.makedirs(dest, exist_ok=True)
    with tarfile.open(fpk) as tf:
        tf.extractall(dest)
    # strip a leading component if present
    return dest

def make_app_tgz(app_dir):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.GNU_FORMAT) as tgz:
        for root, dirs, files in os.walk(app_dir):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            for fn in files:
                if fn in EXCLUDE_FILES or fn.endswith(".pyc"):
                    continue
                full = os.path.join(root, fn)
                arc = os.path.relpath(full, app_dir)
                ti = tgz.gettarinfo(full, arcname=arc)
                ti.mode = 0o755 if fn.endswith(".py") else (ti.mode or 0o644)
                with open(full, "rb") as f:
                    tgz.addfile(ti, f)
    return buf.getvalue()

def main():
    rm_tree(STAGE)
    extract_all(SRC_FPK, STAGE)
    # overlay updated app/
    for root, dirs, files in os.walk(APP_SRC):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for fn in files:
            if fn.endswith(".pyc"):
                continue
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, APP_SRC)
            dst = os.path.join(STAGE, "app", rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(full, dst)
    # ensure no __pycache__ leaked
    for rp, _, fs in os.walk(os.path.join(STAGE, "app")):
        if "__pycache__" in rp:
            shutil.rmtree(rp)
    # rebuild app.tgz
    app_tgz = make_app_tgz(os.path.join(STAGE, "app"))
    app_md5 = hashlib.md5(app_tgz).hexdigest()
    with open(os.path.join(STAGE, "app.tgz"), "wb") as f:
        f.write(app_tgz)
    # update manifest checksum
    mpath = os.path.join(STAGE, MANIFEST_NAME)
    mtxt = open(mpath, "r", encoding="utf-8").read()
    mtxt = re.sub(r"(checksum\s*=\s*)\S+", r"\g<1>%s" % app_md5, mtxt, count=1)
    open(mpath, "w", encoding="utf-8").write(mtxt)
    # build outer fpk (deterministic order). NOTE: only app.tgz, never the
    # expanded app/ directory (fnOS installs by extracting app.tgz).
    members = ["app.tgz", "cmd", "config", "manifest", "wizard",
               "ICON.PNG", "ICON_256.PNG"]
    rm_tree(os.path.join(ROOT, "_fpk_tmp"))
    os.makedirs(os.path.join(ROOT, "_fpk_tmp"), exist_ok=True)
    tmp_app = os.path.join(ROOT, "_fpk_tmp", "app.tgz")
    with open(tmp_app, "wb") as f:
        f.write(app_tgz)
    # outer tar.gz
    with tarfile.open(OUT_FPK, "w:gz", format=tarfile.GNU_FORMAT) as outer:
        # place app.tgz first
        order = ["app.tgz"] + [m for m in members if m != "app.tgz"]
        for name in order:
            p = os.path.join(STAGE, name)
            if os.path.isdir(p):
                for r, dirs, files in os.walk(p):
                    dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
                    for fn in files:
                        if fn.endswith(".pyc"):
                            continue
                        full = os.path.join(r, fn)
                        arc = os.path.relpath(full, STAGE)
                        outer.add(full, arcname=arc)
            else:
                outer.add(p, arcname=name)

    # verify
    with tarfile.open(OUT_FPK) as tf:
        names = tf.getnames()
    at = open(os.path.join(STAGE, "app.tgz"), "rb").read()
    print("app.tgz md5 :", hashlib.md5(at).hexdigest())
    print("members    :", sorted(names)[:20])
    m2 = open(mpath).read()
    cm = re.search(r"checksum\s*=\s*(\S+)", m2).group(1)
    print("manifest ck:", cm)
    print("MATCH      :", cm == hashlib.md5(at).hexdigest())
    print("fpk size   :", os.path.getsize(OUT_FPK))
    print("BUILD OK")

if __name__ == "__main__":
    main()
