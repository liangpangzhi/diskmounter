import os  # 用于读取 NAS_SSH_PASS / APP_PASS 环境变量（避免明文口令入库）
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pre-flight: connect to FnOS NAS, check sudo, capture running process env, disk topology."""
import paramiko, sys, time

HOST = "192.168.5.11"
PORT = 22
USER = "liang"
PASS = os.environ.get("NAS_SSH_PASS", "")  # 已从代码移除明文口令；部署前请 export NAS_SSH_PASS=你的密码

APP_DIR = "/usr/local/apps/@appcenter/diskmounter"
VAR_DIR = "/usr/local/apps/@appdata/diskmounter"

def ssh_connect():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, port=PORT, username=USER, password=PASS, timeout=15,
              look_for_keys=False, allow_agent=False)
    return c

def run(c, cmd, sudo=False, pwd=PASS, timeout=60):
    if sudo:
        # use -S and feed password on stdin; single level to avoid nested hangs
        cmd = "echo '%s' | sudo -S bash -lc %s" % (pwd, __import__('shlex').quote(cmd))
    stdin, stdout, stderr = c.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode('utf-8', 'replace')
    err = stderr.read().decode('utf-8', 'replace')
    rc = stdout.channel.recv_exit_status()
    return rc, out, err

def main():
    c = ssh_connect()
    print("=== SSH connected as %s ===" % USER)

    # 1) sudo availability
    rc, out, err = run(c, "id -a")
    print("\n[user id]", rc, out.strip())

    rc, out, err = run(c, "true", sudo=True)
    print("[sudo test] rc=%s err=%s" % (rc, err.strip()[:200]))
    if rc != 0:
        print("!! sudo failed -> will rely on provided password via -S (already used)")

    # 2) locate running diskm.py
    rc, out, err = run(c, "ps -eo pid,ppid,user,cmd | grep -E '[d]iskm.py' ; echo '---'; "
                          "ls -l %s/bin/diskm.py %s/www/index.html" % (APP_DIR, APP_DIR))
    print("\n[running + target files]\n%s%s" % (out, err))

    # 3) capture TRIM env of running process (for restart)
    rc, out, err = run(c,
        "for p in $(pgrep -f 'bin/diskm.py'); do echo \"== PID $p ==\"; "
        "sudo strings /proc/$p/environ | grep -E '^TRIM_' ; done", sudo=False)
    # Note: reading /proc/$p/environ may need root; try with sudo
    if "TRIM_" not in out:
        rc, out2, err2 = run(c,
            "for p in $(pgrep -f 'bin/diskm.py'); do echo \"== PID $p ==\"; "
            "sudo bash -c 'strings /proc/$p/environ' | grep -E '^TRIM_' ; done", sudo=False)
        out = out2; err = err2
    print("\n[TRIM env of running process]\n%s%s" % (out, err))

    # 4) disk topology (for expected classification)
    rc, out, err = run(c,
        "echo '== /sys/block =='; ls /sys/block; "
        "echo '== mdstat =='; cat /proc/mdstat | grep -E 'md[0-9]|active'; "
        "echo '== mounts (non-sys) =='; mount | grep -E '/dev/(sd|nvme|hd|vd)' ", sudo=False)
    print("\n[disk topology]\n%s%s" % (out, err))

    # 5) hdparm availability
    rc, out, err = run(c, "ls -l /sbin/hdparm /usr/sbin/hdparm 2>/dev/null; command -v hdparm; "
                          "hdparm -V 2>/dev/null | head -1", sudo=False)
    print("\n[hdparm]\n%s%s" % (out, err))

    c.close()
    print("\n=== pre-flight done ===")

if __name__ == "__main__":
    main()
