import os  # 用于读取 NAS_SSH_PASS / APP_PASS 环境变量（避免明文口令入库）
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deep topology + env capture. Single sudo for /proc environ read."""
import paramiko, shlex

HOST="192.168.5.11"; PORT=22; USER="liang"; PASS=os.environ.get("NAS_SSH_PASS","")  # 已从代码移除明文口令；部署前请 export NAS_SSH_PASS=你的密码

def ssh_connect():
    c=paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST,port=PORT,username=USER,password=PASS,timeout=15,look_for_keys=False,allow_agent=False)
    return c

def run(c,cmd,sudo=True,timeout=90):
    if sudo:
        cmd="echo %s | sudo -S bash -lc %s" % (shlex.quote(PASS), shlex.quote(cmd))
    sin,so,se=c.exec_command(cmd,timeout=timeout)
    out=so.read().decode('utf-8','replace'); err=se.read().decode('utf-8','replace')
    rc=so.channel.recv_exit_status(); return rc,out,err

def main():
    c=ssh_connect()
    probe = (
        "echo '===== full /proc/mdstat ====='; cat /proc/mdstat; "
        "echo; echo '===== lsblk (disks) ====='; lsblk -d -o NAME,SIZE,TYPE,ROTA,MOUNTPOINT 2>/dev/null || lsblk -d; "
        "echo; echo '===== pvs ====='; pvs 2>/dev/null; "
        "echo '===== vgs ====='; vgs 2>/dev/null; "
        "echo '===== lvs ====='; lvs 2>/dev/null; "
        "echo; echo '===== zpool status ====='; zpool status 2>/dev/null | head -40 || echo 'no zfs'; "
        "echo; echo '===== mounts with sd/nvme ====='; mount | grep -E '/dev/(sd|nvme|hd|vd|md|dm)'; "
        "echo; echo '===== /etc/fstab relevant ====='; grep -E 'sd|nvme|md|dm' /etc/fstab 2>/dev/null"
    )
    rc,out,err=run(c,probe)
    print(out)
    if err.strip(): print("ERR:\n",err[:500])

    # capture TRIM env of PID 8405 (single sudo, no nested sudo)
    rc,out,err=run(c, "strings /proc/8405/environ | grep -E '^TRIM_'")
    print("\n===== TRIM env PID 8405 =====\n"+out)
    if err.strip(): print("ERR:",err[:300])

    # verify a safe read of power state on a couple disks (must NOT spin down)
    rc,out,err=run(c, "for d in sda sdb sdc sdf; do echo -n \"$d: \"; /sbin/hdparm -C /dev/$d 2>/dev/null | grep -i 'drive state' || echo 'n/a'; done")
    print("\n===== hdparm -C (read-only power query) =====\n"+out)
    if err.strip(): print("ERR:",err[:300])
    c.close()

if __name__=="__main__":
    main()
