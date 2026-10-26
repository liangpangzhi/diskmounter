import os  # 用于读取 NAS_SSH_PASS / APP_PASS 环境变量（避免明文口令入库）
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Redeploy only the backend diskm.py (hot-upgrade) and restart via direct launch.
Backs up the current backend, uploads the new one, compiles, restarts with TRIM env.
"""
import paramiko, shlex, time, os

HOST="192.168.5.11"; PORT=22; USER="liang"; PASS=os.environ.get("NAS_SSH_PASS","")  # 已从代码移除明文口令；部署前请 export NAS_SSH_PASS=你的密码
APP_DIR="/usr/local/apps/@appcenter/diskmounter"
VAR_DIR="/usr/local/apps/@appdata/diskmounter"
LOCAL=r"D:/挂载和卸载飞牛磁盘工具/_unpack/app/bin/diskm.py"
TS=time.strftime("%Y%m%d%H%M%S")

RESTART_SH = r"""#!/bin/bash
set -u
APP_DIR=/usr/local/apps/@appcenter/diskmounter
VAR_DIR=/usr/local/apps/@appdata/diskmounter
LOG=$VAR_DIR/backend.log
pkill -f "bin/diskm.py" 2>/dev/null
sleep 2
rm -f "$VAR_DIR/app.pid"
export TRIM_APPDEST="$APP_DIR"
export TRIM_PKGVAR="$VAR_DIR"
export TRIM_PKGETC=/usr/local/apps/@appconf/diskmounter
export TRIM_PKGMETA=/vol1/@appmeta/diskmounter
export TRIM_PKGHOME=/usr/local/apps/@apphome/diskmounter
export TRIM_PKGTMP=/usr/local/apps/@apptemp/diskmounter
export TRIM_TEMP_LOGFILE=/tmp/diskmounter.log."$(date +%s)"
export TRIM_APPNAME=diskmounter
export TRIM_SERVICE_PORT=5688
export TRIM_RUN_USERNAME=root
export TRIM_RUN_UID=0
nohup /usr/bin/python3 "$APP_DIR/bin/diskm.py" >> "$LOG" 2>&1 &
echo $! > "$VAR_DIR/app.pid"
sleep 3
if kill -0 "$(cat "$VAR_DIR/app.pid")" 2>/dev/null; then echo "STARTED_OK pid=$(cat "$VAR_DIR/app.pid")"; else echo "START_FAILED"; tail -n 25 "$LOG"; fi
"""

def connect():
    c=paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST,port=PORT,username=USER,password=PASS,timeout=15,look_for_keys=False,allow_agent=False)
    return c

def run(c,cmd,sudo=True,timeout=120):
    if sudo:
        cmd="echo %s | sudo -S bash -lc %s" % (shlex.quote(PASS), shlex.quote(cmd))
    sin,so,se=c.exec_command(cmd,timeout=timeout)
    out=so.read().decode('utf-8','replace'); err=se.read().decode('utf-8','replace')
    rc=so.channel.recv_exit_status(); return rc,out,err

def main():
    c=connect()
    # backup current
    rc,out,err=run(c,"cp -p %s/bin/diskm.py %s/bin/diskm.py.bak.%s && ls -l %s/bin/diskm.py.bak.%s"
                   % (APP_DIR,APP_DIR,TS,APP_DIR,TS))
    print("[backup]",rc,out.strip(),err.strip()[:150])
    # upload
    sftp=c.open_sftp(); sftp.put(LOCAL,"/tmp/diskm_new_%s.py"%TS); sftp.close()
    rc,out,err=run(c,"cp /tmp/diskm_new_%s.py %s/bin/diskm.py && chmod 755 %s/bin/diskm.py && python3 -m py_compile %s/bin/diskm.py && echo COMPILE_OK && wc -c %s/bin/diskm.py"
                   % (TS,APP_DIR,APP_DIR,APP_DIR,APP_DIR))
    print("[install]",rc,out.strip(),err.strip()[:200])
    # restart script
    local=r"D:/挂载和卸载飞牛磁盘工具/_restart.sh"
    with open(local,"w",newline="\n") as f: f.write(RESTART_SH)
    sftp=c.open_sftp(); sftp.put(local,"/tmp/restart_%s.sh"%TS); sftp.close()
    rc,out,err=run(c,"bash /tmp/restart_%s.sh"%TS)
    print("[restart]",rc,"\n",out,err[:200])
    time.sleep(2)
    rc,out,err=run(c,"ps -eo pid,user,cmd|grep -E '[d]iskm.py'; (ss -ltnp 2>/dev/null|grep 5688)||echo 'no-port'; curl -s http://127.0.0.1:5688/ | grep -c 硬盘停转")
    print("[verify]\n",out,err[:150])
    c.close()

if __name__=="__main__":
    main()
