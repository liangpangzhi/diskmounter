import os  # 用于读取 NAS_SSH_PASS / APP_PASS 环境变量（避免明文口令入库）
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Robust restart of diskmounter backend: write a script file, SFTP it, run via sudo.
Avoids nested-quote breakage. Also recovers the service if it is currently down.
"""
import paramiko, shlex, time, os

HOST="192.168.5.11"; PORT=22; USER="liang"; PASS=os.environ.get("NAS_SSH_PASS","")  # 已从代码移除明文口令；部署前请 export NAS_SSH_PASS=你的密码
APP_DIR="/usr/local/apps/@appcenter/diskmounter"
VAR_DIR="/usr/local/apps/@appdata/diskmounter"
TS=time.strftime("%Y%m%d%H%M%S")

RESTART_SH = r"""#!/bin/bash
set -u
APP_DIR=/usr/local/apps/@appcenter/diskmounter
VAR_DIR=/usr/local/apps/@appdata/diskmounter
ETC_DIR=/usr/local/apps/@appconf/diskmounter
META_DIR=/vol1/@appmeta/diskmounter
HOME_DIR=/usr/local/apps/@apphome/diskmounter
TMP_DIR=/usr/local/apps/@apptemp/diskmounter
LOG=$VAR_DIR/backend.log
mkdir -p "$VAR_DIR" "$ETC_DIR" "$META_DIR" "$HOME_DIR" "$TMP_DIR"
echo "== before =="
ps -eo pid,user,cmd | grep '[d]iskm.py' || echo "no running process"
# 1) kill any existing instance (avoid port conflict)
pkill -f "bin/diskm.py" 2>/dev/null
sleep 2
rm -f "$VAR_DIR/app.pid"
# 2) launch directly with the TRIM_* env fnOS would set
export TRIM_APPDEST="$APP_DIR"
export TRIM_PKGVAR="$VAR_DIR"
export TRIM_PKGETC="$ETC_DIR"
export TRIM_PKGMETA="$META_DIR"
export TRIM_PKGHOME="$HOME_DIR"
export TRIM_PKGTMP="$TMP_DIR"
export TRIM_TEMP_LOGFILE=/tmp/diskmounter.log."$(date +%s)"
export TRIM_APPNAME=diskmounter
export TRIM_SERVICE_PORT=5688
export TRIM_RUN_USERNAME=root
export TRIM_RUN_UID=0
nohup /usr/bin/python3 "$APP_DIR/bin/diskm.py" >> "$LOG" 2>&1 &
echo $! > "$VAR_DIR/app.pid"
sleep 3
echo "== after =="
if kill -0 "$(cat "$VAR_DIR/app.pid")" 2>/dev/null; then
  echo "STARTED_OK pid=$(cat "$VAR_DIR/app.pid")"
else
  echo "START_FAILED"; tail -n 25 "$LOG"
fi
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
    # write script locally with UNIX LF line endings (CRLF breaks bash)
    local=r"D:/挂载和卸载飞牛磁盘工具/_restart.sh"
    with open(local,"w",newline="\n") as f:
        f.write(RESTART_SH)
    sftp=c.open_sftp(); sftp.put(local,"/tmp/restart_%s.sh"%TS); sftp.close()
    # run it (single sudo, script file -> no nested quote issue)
    rc,out,err=run(c,"bash /tmp/restart_%s.sh"%TS)
    print("[restart rc=%s]\n%s\nERR:%s" % (rc,out,err[:400]))
    time.sleep(2)
    # verify
    rc,out,err=run(c,
        "echo '== procs =='; ps -eo pid,user,cmd|grep -E '[d]iskm.py'; "
        "echo '== port 5688 =='; (ss -ltnp 2>/dev/null | grep 5688) || (netstat -ltnp 2>/dev/null | grep 5688) || echo 'no ss/netstat'; "
        "echo '== new tab served? =='; curl -s http://127.0.0.1:5688/ | grep -c '硬盘停转'")
    print("\n[verify]\n",out, err[:200])
    c.close()

if __name__=="__main__":
    main()
