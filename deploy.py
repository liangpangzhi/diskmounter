import os  # 用于读取 NAS_SSH_PASS / APP_PASS 环境变量（避免明文口令入库）
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deploy diskmounter 1.3.0 to FnOS NAS via hot-upgrade:
   backup originals -> SFTP new files -> restart backend via cmd/main.
"""
import paramiko, shlex, time, os

HOST="192.168.5.11"; PORT=22; USER="liang"; PASS=os.environ.get("NAS_SSH_PASS","")  # 已从代码移除明文口令；部署前请 export NAS_SSH_PASS=你的密码
APP_DIR="/usr/local/apps/@appcenter/diskmounter"
VAR_DIR="/usr/local/apps/@appdata/diskmounter"
LOCAL_DISKM=r"D:/挂载和卸载飞牛磁盘工具/_unpack/app/bin/diskm.py"
LOCAL_INDEX=r"D:/挂载和卸载飞牛磁盘工具/_unpack/app/www/index.html"
TS=time.strftime("%Y%m%d%H%M%S")

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
    print("=== SSH OK ===")

    # 1) backup
    rc,out,err=run(c,
        "cp -p %s/bin/diskm.py %s/bin/diskm.py.bak.%s; "
        "cp -p %s/www/index.html %s/www/index.html.bak.%s; "
        "ls -l %s/bin/diskm.py.bak.%s %s/www/index.html.bak.%s"
        % (APP_DIR,APP_DIR,TS,APP_DIR,APP_DIR,TS,APP_DIR,TS,APP_DIR,TS))
    print("[backup]", rc, out.strip(), err.strip()[:200])

    # 2) SFTP upload to /tmp
    sftp=c.open_sftp()
    sftp.put(LOCAL_DISKM, "/tmp/diskm_new_%s.py" % TS)
    sftp.put(LOCAL_INDEX, "/tmp/index_new_%s.html" % TS)
    sftp.close()
    print("[sftp] uploaded to /tmp")

    # 3) move into place (root-owned dirs)
    rc,out,err=run(c,
        "cp /tmp/diskm_new_%s.py %s/bin/diskm.py && "
        "cp /tmp/index_new_%s.html %s/www/index.html && "
        "chmod 755 %s/bin/diskm.py && "
        "python3 -m py_compile %s/bin/diskm.py && echo COMPILE_OK && "
        "wc -c %s/bin/diskm.py %s/www/index.html"
        % (TS,APP_DIR,TS,APP_DIR,APP_DIR,APP_DIR,APP_DIR,APP_DIR))
    print("[install]", rc, out.strip(), err.strip()[:300])

    # 4) restart via cmd/main (kill any existing, start fresh with TRIM env)
    rc,out,err=run(c,
        "pkill -f 'bin/diskm.py' 2>/dev/null; sleep 2; "
        "rm -f %s/app.pid; "
        "export TRIM_APPDEST=%s TRIM_PKGVAR=%s TRIM_TEMP_LOGFILE=/tmp/diskmounter.log.%s "
        "TRIM_APPNAME=diskmounter TRIM_SERVICE_PORT=5688; "
        "%s/cmd/main start; sleep 2; "
        "if [ -f %s/app.pid ] && kill -0 $(cat %s/app.pid) 2>/dev/null; then "
        "echo STARTED_OK pid=$(cat %s/app.pid); else echo START_FAILED; fi"
        % (VAR_DIR,APP_DIR,VAR_DIR,TS,APP_DIR,VAR_DIR,VAR_DIR,VAR_DIR))
    print("[restart]", rc, out.strip(), err.strip()[:300])

    # 5) verify
    time.sleep(2)
    rc,out,err=run(c, "echo '== procs =='; ps -eo pid,user,cmd|grep -E '[d]iskm.py'; "
                      "echo '== port 5688 =='; (ss -ltnp 2>/dev/null | grep 5688) || (netstat -ltnp 2>/dev/null | grep 5688); "
                      "echo '== served frontend has new tab? =='; "
                      "curl -s http://127.0.0.1:5688/ | grep -c '硬盘停转' ")
    print("[verify]\n", out, err.strip()[:200])
    c.close()
    print("=== deploy done ===")

if __name__=="__main__":
    main()
