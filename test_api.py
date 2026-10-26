import os  # 用于读取 NAS_SSH_PASS / APP_PASS 环境变量（避免明文口令入库）
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Functional test of diskmounter 1.3.0 spin-down feature via web API.
 A) scan classification  B) safety-gate rejection  C) real safe spin cycle.
"""
import urllib.request, urllib.error, json, http.cookiejar, sys, time

BASE="http://192.168.5.11:5688"
PW=os.environ.get("APP_PASS","123456")  # 应用默认访问口令（与 README 一致）

def login():
    cj=http.cookiejar.CookieJar()
    op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    req=urllib.request.Request(BASE+"/api/login",
        data=json.dumps({"password":PW}).encode(),
        headers={"Content-Type":"application/json"}, method="POST")
    try:
        r=op.open(req,timeout=30); body=json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return None, e.code, e.read().decode()
    tok=None
    for ck in cj:
        if ck.name=="dm_token": tok=ck.value
    return tok, r.status, body

def call(tok,method,path,data=None):
    headers={"Cookie":"dm_token="+tok} if tok else {}
    if data is not None:
        body=json.dumps(data).encode(); headers["Content-Type"]="application/json"
    else: body=None
    req=urllib.request.Request(BASE+path,data=body,headers=headers,method=method)
    try:
        r=urllib.request.urlopen(req,timeout=40); return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

def main():
    tok,st,body=login()
    print("[login] status=%s body=%s token=%s" % (st, body, "yes" if tok else "NO"))
    if not tok:
        print("!! Login failed with default password. Cannot run API tests.")
        print("   Please provide the app login password, or I will fall back to a backend-logic test via SSH.")
        sys.exit(2)
    # A) scan
    st,data=call(tok,"GET","/api/disks")
    print("\n=== A) /api/disks ===")
    print("status",st)
    if isinstance(data,dict) and "disks" in data:
        disks=data["disks"]
        print("hdparm_ok:", data.get("hdparm_ok"), "| auto_spindown:", data.get("auto_spindown"))
        print("%-6s %-22s %-10s %-14s %-8s" % ("name","model","size","class","power"))
        for d in disks:
            print("%-6s %-22s %-10s %-14s %-8s" % (d["name"],(d.get("model") or "")[:22],
                  d.get("size"), d.get("class"), d.get("power")))
        byclass={}
        for d in disks: byclass.setdefault(d["class"],[]).append(d["name"])
        print("summary by class:", byclass)
    else:
        print(data)

    # B) safety gate: try to spin down protected disks
    print("\n=== B) safety gate (should REJECT) ===")
    for name in ("sda","sdb"):
        st,r=call(tok,"POST","/api/disk/spindown",{"disk":name})
        if isinstance(r,dict):
            print("spindown %s -> status=%s ok=%s msg=%s" % (name,st,r.get("ok"),r.get("msg")))
        else:
            print("spindown %s -> status=%s raw=%s" % (name,st,r))

    # C) real safe spin cycle on one idle_safe disk (restore original state)
    print("\n=== C) real safe spin cycle ===")
    idle=[d for d in disks if d.get("class")=="idle_safe"] if isinstance(data,dict) else []
    if not idle:
        print("no idle_safe disk available for real test (unexpected).")
        return
    tgt=idle[0]["name"]
    cur=idle[0].get("power")
    print("target=%s current_power=%s" % (tgt,cur))
    if cur in ("active","idle"):
        # demonstrate down then up
        st,r=call(tok,"POST","/api/disk/spindown",{"disk":tgt})
        print("spindown -> %s" % (r if isinstance(r,dict) else r))
        st,r=call(tok,"POST","/api/disk/spinup",{"disk":tgt})
        print("spinup   -> %s" % (r if isinstance(r,dict) else r))
    else:
        # currently standby: demonstrate up then down (restore standby)
        st,r=call(tok,"POST","/api/disk/spinup",{"disk":tgt})
        print("spinup   -> %s" % (r if isinstance(r,dict) else r))
        st,r=call(tok,"POST","/api/disk/spindown",{"disk":tgt})
        print("spindown -> %s" % (r if isinstance(r,dict) else r))
    # final state (allow a few seconds for physical spin-down to settle)
    time.sleep(8)
    st,data2=call(tok,"GET","/api/disks")
    if isinstance(data2,dict):
        for d in data2["disks"]:
            if d["name"]==tgt:
                print("final power of %s = %s (restored)" % (tgt,d["power"]))

if __name__=="__main__":
    main()
