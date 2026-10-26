#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
磁盘挂载管家 - fnOS 存储池挂载/安全卸载后端
支持: mdadm RAID -> LVM -> 文件系统(btrfs/ext4...) 存储池 与 ZFS 存储池

安全策略:
  - 系统盘(根文件系统/EFI所在盘)与其上的存储池强制排除, 不可操作
  - 可移动磁盘(USB)与挂载在 /vol00 下的设备强制排除
  - 卸载前通过 /proc 扫描占用进程(句柄/工作目录), 存在占用即拒绝卸载并列出进程
  - 用户选择"强制卸载"时: 先优雅停止占用进程(systemd 服务)或终止(其他进程),
    卸载完成后自动重启这些服务; 绝不跳过占用检查直接卸载
  - 卸载流程: sync -> umount -l -> vgchange -an -> mdadm --stop (ZFS: umount -l -> zpool export)
    (飞牛定制内核下共享挂载存在传播引用, 普通 umount 会返回 busy, 惰性卸载可安全脱离;
     停止占用进程后无写入在进行, 惰性卸载不会丢数据)
  - 期望状态持久化, 启动后周期 reconcile, 重启后自动维持
  - 已知存储池信息持久化(known), 阵列停止后仍可被应用找到并重新挂载
"""
import hashlib
import json
import os
import re
import shutil
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_NAME = os.environ.get("TRIM_APPNAME", "diskmounter")
VAR_DIR = os.environ.get("TRIM_PKGVAR", "/var/apps/%s/var" % APP_NAME)
APP_DIR = os.environ.get("TRIM_APPDEST", "/var/apps/%s" % APP_NAME)
STATE_PATH = os.path.join(VAR_DIR, "state.json")
CONFIG_PATH = os.path.join(VAR_DIR, "config.json")
SESSIONS_PATH = os.path.join(VAR_DIR, "sessions.json")
OPSLOG_PATH = os.path.join(VAR_DIR, "ops.log")
INDEX_PATH = os.path.join(APP_DIR, "www", "index.html")
PORT = int(os.environ.get("TRIM_SERVICE_PORT", "5688"))
RECONCILE_INTERVAL = 30  # 开机后尽快恢复期望状态
RECONCILE_INITIAL_DELAY = 10

# mdadm 开机自动组装抑制(让已卸载硬盘重启时不启动)
MDADM_CONF = "/etc/mdadm/mdadm.conf"
MDADM_BEGIN = "# >>> diskmounter managed >>>"
MDADM_END = "# <<< diskmounter managed <<<"

_state_lock = threading.Lock()
_op_lock = threading.Lock()
_sessions = {}
_sessions_lock = threading.Lock()

# reconcile 时允许自动停止重启的系统服务(其余占用一律跳过, 不自动杀)
SAFE_SERVICE_UNITS = {
    "share_service", "filestor_service", "extdev_service", "finder_service",
    "smbd", "nmbd", "winbindd", "trim_http_cgi", "diskpowerd",
}

# ---------------------------------------------------------------- utilities

def sh(args, timeout=60):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or ""), (p.stderr or "")
    except Exception as e:  # noqa: BLE001
        return 127, "", str(e)


def oplog(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        os.makedirs(VAR_DIR, exist_ok=True)
        with open(OPSLOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data, mode=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def base_disk(dev):
    """sda1 / nvme0n1p2 -> sda / nvme0n1; md0 等虚拟设备原样返回"""
    dev = dev.replace("/dev/", "")
    if dev.startswith(("md", "loop", "ram", "dm-")):
        return dev
    m = re.match(r"^(nvme\d+n\d+)p\d+$", dev)
    if m:
        return m.group(1)
    return re.sub(r"p?\d+$", "", dev)


def disk_size(name):
    try:
        with open("/sys/block/%s/size" % name) as f:
            return int(f.read().strip()) * 512
    except (OSError, ValueError):
        return 0


def human(n):
    if not n:
        return "-"
    u = ("B", "KB", "MB", "GB", "TB", "PB")
    i = 0
    n = float(n)
    while n >= 1024 and i < len(u) - 1:
        n /= 1024.0
        i += 1
    return ("%.1f%s" % (n, u[i])) if i else "%dB" % n

# ---------------------------------------------------------------- mounts

def read_mounts():
    """[(dev_src, mountpoint, fstype, opts)]"""
    out = []
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 4:
                    out.append((parts[0], parts[1], parts[2], parts[3]))
    except OSError:
        pass
    return out


def system_disks():
    """必须排除的磁盘: 根文件系统/EFI 所在盘 + /vol00 下设备 + 可移动磁盘"""
    dis = set()
    for src, mp, _fst, _opt in read_mounts():
        if mp in ("/", "/boot", "/boot/efi") and src.startswith("/dev/"):
            dis.add(base_disk(src))
        if mp.startswith("/vol00") and src.startswith("/dev/"):
            dis.add(base_disk(src))
    try:
        names = sorted(os.listdir("/sys/block"))
    except OSError:
        names = []
    for name in names:
        try:
            with open("/sys/block/%s/removable" % name) as f:
                if f.read().strip() == "1":
                    dis.add(name)
        except OSError:
            pass
    return dis


def md_arrays():
    """解析 /proc/mdstat -> {mdN: {"active": bool, "members": [sdX1,...]}}"""
    arrays = {}
    try:
        with open("/proc/mdstat") as f:
            lines = f.read().splitlines()
    except OSError:
        return arrays
    cur = None
    for ln in lines:
        m = re.match(r"^(md\d+)\s*:\s*(\S+)\s+(.*)$", ln)
        if m:
            cur = m.group(1)
            members = re.findall(r"(sd[a-z]+\d+|nvme\d+n\d+p\d+)", m.group(3))
            arrays[cur] = {"active": m.group(2) == "active",
                           "members": [x.split("[")[0] for x in members]}
        elif ln and not ln.startswith(("unused", " ", "\t")):
            cur = None
    return arrays


def dm_path(vg, lv):
    esc = lambda s: s.replace("-", "--")  # noqa: E731
    return "/dev/mapper/%s-%s" % (esc(vg), esc(lv))


# ---------------------------------------------------------------- disk spin-down (安全停转)

# 安全停转的核心约束(绝对保护数据/硬盘):
#   - 系统盘(根/EFI/boot)            -> 永远禁止停转
#   - 属于"活跃"存储池/阵列/ZFS 的盘 -> 永远禁止停转(否则存储池降级/损坏)
#   - 当前已挂载(使用中)的独立盘     -> 禁止停转, 需先卸载
#   - 仅"空闲且不属于任何活跃池"的盘  -> 允许停转(停转不会损坏数据, 访问时自动唤醒)
# 停转使用 hdparm -y(待机, 自动唤醒); SAS 盘回退 sg_start --stop。
# 唤醒使用非破坏性读取首扇区(dd), 触发自旋启动; 不改变任何数据。

DISK_RE = r"^(sd[a-z]+|nvme\d+n\d+|hd[a-z]+|vd[a-z]+)$"


def physical_disks():
    """系统上全部真实硬盘(排除分区/loop/ram/dm/md/zram/光驱), 排除可移动盘"""
    out = []
    try:
        names = sorted(os.listdir("/sys/block"))
    except OSError:
        return out
    for name in names:
        if name.startswith(("loop", "ram", "dm-", "md", "zram", "sr", "fd")):
            continue
        if not re.match(DISK_RE, name):
            continue
        try:
            with open("/sys/block/%s/removable" % name) as f:
                if f.read().strip() == "1":
                    continue
        except OSError:
            pass
        out.append(name)
    return out


def disk_model(name):
    try:
        with open("/sys/block/%s/device/model" % name) as f:
            return f.read().strip()
    except OSError:
        return ""


def active_pool_disks():
    """当前被活跃存储池/阵列使用的硬盘集合(禁止停转)"""
    disks = set()
    # 所有活跃 md 阵列的成员盘
    for _n, info in md_arrays().items():
        if info.get("active"):
            for m in info.get("members", []):
                disks.add(base_disk(m))
    # 活跃 LVM 卷组所用 PV 所在硬盘
    rc, out, _ = sh(["vgs", "--noheadings", "-o", "vg_name,vg_attr"])
    active_vgs = set()
    if rc == 0:
        for ln in out.splitlines():
            parts = [x.strip() for x in ln.split()]
            if len(parts) >= 2 and parts[1] and parts[1][0] == "a":
                active_vgs.add(parts[0])
    if active_vgs:
        rc2, vout, _ = sh(["pvs", "--noheadings", "-o", "pv_name,vg_name"])
        if rc2 == 0:
            for ln in vout.splitlines():
                parts = [x.strip() for x in ln.split()]
                if len(parts) >= 2 and parts[1] in active_vgs and parts[0].startswith("/dev/"):
                    disks.add(base_disk(parts[0]))
    # 已导入 ZFS 池成员盘
    rc, out, _ = sh(["zpool", "list", "-H", "-o", "name"])
    if rc == 0:
        for pn in out.split():
            _rc, vout, _ = sh(["zpool", "status", "-L", "-v", pn])
            for ln in vout.splitlines():
                toks = ln.split()
                if toks and re.match(r"^(sd[a-z]+|nvme\d+n\d+)(p?\d+)?$", toks[0]):
                    disks.add(base_disk(toks[0]))
    return disks


def mounted_standalone_disks(excluded):
    """已挂载且不属于活跃存储池的独立硬盘(使用中, 不得停转)"""
    disks = set()
    for src, _mp, _f, _o in read_mounts():
        if not src.startswith("/dev/"):
            continue
        if src.startswith(("/dev/mapper/", "/dev/zd", "/dev/md", "/dev/dm-")):
            continue
        bd = base_disk(src)
        if not re.match(DISK_RE, bd):
            continue
        if bd in excluded:
            continue
        disks.add(bd)
    return disks


def disk_power(name):
    """查询电源状态: active/idle/standby/unknown (CHECK POWER MODE 不会唤醒硬盘)"""
    rc, out, _ = sh([hdparm_bin(), "-C", "/dev/" + name], timeout=15)
    if rc != 0:
        return "unknown"
    m = re.search(r"drive state is:?\s*(\w+)", out)
    if not m:
        return "unknown"
    s = m.group(1).lower()
    return {"active": "active", "idle": "idle", "standby": "standby",
            "sleeping": "standby", "unknown": "unknown"}.get(s, "unknown")


def sg_device_for(name):
    """SAS 盘 /dev/sdX 对应的 /dev/sgN (sg_start 停转用)"""
    p = "/sys/block/%s/device/scsi_generic" % name
    try:
        for entry in os.listdir(p):
            return "/dev/" + entry
    except OSError:
        pass
    return ""


def hdparm_bin():
    """定位 hdparm 可执行文件: 优先绝对路径, 避免不同进程 PATH 差异导致找不到。"""
    for c in ("/usr/sbin/hdparm", "/sbin/hdparm", "hdparm"):
        if c.startswith("/"):
            if os.path.exists(c):
                return c
        else:
            p = shutil.which(c)
            if p:
                return p  # 返回解析后的绝对路径, 而非裸命令名
    return "hdparm"


def _valid_disk(name):
    return bool(re.match(DISK_RE, name or ""))


def _find_bin(name):
    """定位系统命令的绝对路径(部分环境 /sbin,/usr/sbin 不在 PATH); 找不到回退裸名"""
    for c in (name, "/sbin/" + name, "/usr/sbin/" + name,
             "/usr/bin/" + name, "/bin/" + name):
        if "/" in c:
            if os.path.exists(c):
                return c
        elif shutil.which(c):
            return c
    return name


def exported_zfs_disks():
    """返回属于『已导出(未导入)ZFS 池』的成员盘名集合。

    这些盘上的 ZFS 元数据仍在(分区 TYPE=zfs_member), 但对应池当前未导入。
    飞牛的 zfs-zed 守护进程会持续运行 `zpool import` 尝试导入该池, 而 import
    扫描会打开这些盘读取 ZFS 标签, 导致它们一旦停转就被立刻唤醒(表现为停不下来)。
    识别它们是为了在前端如实提示, 避免被误判为可安静停转的空闲盘。
    无 zfs / 无可导入池时安全返回空集。
    """
    res = set()
    # 1) 已导入(活跃)的池 -> 其成员盘属于活跃池, 由 active_pool_disks() 处理, 不在此列
    # 2) 可导入(已导出)的池设备 GUID
    rc2, imp_out, _ = sh([_find_bin("zpool"), "import"], timeout=25)
    if not imp_out.strip() or "no pools available" in imp_out:
        return res
    guid_re = re.compile(
        r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b")
    guids = {g.lower() for g in guid_re.findall(imp_out)}
    if not guids:
        return res
    # 3) 找出所有 zfs_member 分区及其 PARTUUID
    rc3, blk, _ = sh([_find_bin("blkid"), "-o", "export"])
    if rc3 != 0 or not blk.strip():
        return res
    dev = None
    zfs_parts = {}
    for ln in blk.splitlines():
        if ln.startswith("DEVNAME="):
            dev = ln.split("=", 1)[1]
        elif ln.startswith("TYPE="):
            if ln.split("=", 1)[1].strip() == "zfs_member" and dev:
                zfs_parts[dev] = None
        elif ln.startswith("PARTUUID="):
            if dev in zfs_parts:
                zfs_parts[dev] = ln.split("=", 1)[1].strip().lower()
    for devname, puuid in zfs_parts.items():
        if puuid and puuid in guids:
            res.add(base_disk(devname))
    return res


def disk_classify(name, sysd=None, pool_disks=None, stand=None, disk_pool=None, exported_zfs=None):
    """返回 (cls, label, pool_name)
    cls: system / pool_active / mounted / pool_exported / idle_safe"""
    if sysd is None:
        sysd = system_disks()
    if name in sysd:
        return "system", "系统盘(受保护)", ""
    if pool_disks is None:
        pool_disks = active_pool_disks()
    if name in pool_disks:
        pn = (disk_pool or {}).get(name, "")
        return "pool_active", "存储池中(活跃)", pn
    if stand is None:
        stand = mounted_standalone_disks(sysd)
    if name in stand:
        return "mounted", "已挂载(使用中)", ""
    if exported_zfs is None:
        exported_zfs = exported_zfs_disks()
    if name in exported_zfs:
        return "pool_exported", "已导出ZFS池(会被唤醒)", ""
    return "idle_safe", "可停转(空闲)", ""


def scan_disks():
    """扫描全部数据盘并做安全分类 + 电源状态"""
    sysd = system_disks()
    pool_disks = active_pool_disks()
    stand = mounted_standalone_disks(sysd)
    exported_zfs = exported_zfs_disks()
    disk_pool = {}
    for p in discover():
        for d in p.get("member_disks", []):
            disk_pool.setdefault(d, p["name"])
    result = []
    for name in physical_disks():
        cls, label, pn = disk_classify(name, sysd, pool_disks, stand, disk_pool, exported_zfs)
        result.append({
            "name": name,
            "model": disk_model(name),
            "size": disk_size(name),
            "class": cls,
            "class_label": label,
            "power": disk_power(name),
            "pool": pn,
        })
    return result


def spin_down_disk(name):
    """安全停转单块硬盘(执行时再次校验, 绝不伤数据)"""
    if not _valid_disk(name):
        return False, "非法设备名"
    sysd = system_disks()
    if name in sysd:
        return False, "系统盘受保护，禁止停转"
    if name in active_pool_disks():
        return False, "该硬盘正属于活跃存储池，禁止停转(否则会损坏/降级存储池)"
    if name in mounted_standalone_disks(sysd):
        return False, "该硬盘当前已挂载使用中，请先到『存储池管理』执行安全卸载"
    oplog("停转硬盘 %s (hdparm -y)" % name)
    rc, _out, err = sh([hdparm_bin(), "-y", "/dev/" + name], timeout=30)
    if rc == 0:
        return True, "已发送停转指令，硬盘将在数秒内停转"
    sg = sg_device_for(name)
    if sg:
        rc, _o2, err2 = sh(["sg_start", "--stop", sg], timeout=30)
        if rc == 0:
            return True, "已发送停转指令(SAS)"
    return False, "停转失败: %s" % ((err or "").strip() or "该硬盘不支持停转或命令不可用")


def spin_up_disk(name):
    """唤醒单块硬盘: 非破坏性读取首扇区触发自旋启动"""
    if not _valid_disk(name):
        return False, "非法设备名"
    oplog("唤醒硬盘 %s (读取首扇区以恢复旋转)" % name)
    # 读取首个扇区(非破坏), 触发硬盘自旋启动; 不修改任何数据
    rc, _o, err = sh(["dd", "if=/dev/" + name, "of=/dev/null", "bs=512", "count=1"],
                     timeout=45)
    if rc != 0:
        sg = sg_device_for(name)
        if sg:
            rc, _o2, err2 = sh(["sg_start", "--start", sg], timeout=45)
    if rc == 0:
        return True, "已发送唤醒指令，硬盘正在自旋启动(约数秒~十余秒)"
    return False, "唤醒失败: %s" % ((err or "").strip() or "未知错误")


def spin_down_members(member_disks, auto=True):
    """对一组成员盘执行停转, 多重安全闸门(系统盘/活跃池盘/已挂载盘一律跳过)"""
    if auto and not get_setting("auto_spindown", True):
        return
    sysd = system_disks()
    pool_disks = active_pool_disks()
    stand = mounted_standalone_disks(sysd)
    for d in member_disks or []:
        if not _valid_disk(d):
            continue
        if d in sysd or d in pool_disks or d in stand:
            continue  # 双保险: 绝不停止系统盘/活跃池盘/已挂载盘
        oplog("卸载后停转成员硬盘 %s" % d)
        rc, _o, _e = sh([hdparm_bin(), "-y", "/dev/" + d], timeout=30)
        if rc != 0:
            sg = sg_device_for(d)
            if sg:
                sh(["sg_start", "--stop", sg], timeout=30)


def wake_members(member_disks):
    """挂载前唤醒成员盘, 避免阵列因盘未就绪而被误判降级"""
    for d in member_disks or []:
        if not _valid_disk(d):
            continue
        oplog("挂载前唤醒成员硬盘 %s" % d)
        sh(["dd", "if=/dev/" + d, "of=/dev/null", "bs=512", "count=1"], timeout=45)


def resume_disk(name):
    """恢复使用: 先唤醒硬盘, 再尝试重新挂载其所属存储池"""
    if not _valid_disk(name):
        return False, "非法设备名"
    spin_up_disk(name)
    target = None
    for p in discover():
        if name in p.get("member_disks", []):
            target = p
            break
    if not target:
        st = load_state()
        for _kn, det in st.get("known", {}).items():
            if name in det.get("member_disks", []):
                target = det
                break
    if not target:
        return True, "已唤醒硬盘(未关联到任何已记录的存储池)"
    pn = target["name"] if isinstance(target, dict) else target
    ok, msg = do_mount(pn)
    return ok, "已唤醒硬盘并" + ("成功恢复挂载" if ok else "尝试恢复挂载失败: " + msg)


# ---------------------------------------------------------------- LVM pools

def lvm_pools(excluded):
    pools = []
    rc, out, _ = sh(["pvs", "--noheadings", "--separator=|", "-o", "pv_name,vg_name"])
    if rc != 0:
        return pools
    vg_pvs = {}
    for ln in out.splitlines():
        parts = [x.strip() for x in ln.split("|")]
        if len(parts) < 2 or not parts[0].startswith("/dev/") or not parts[1]:
            continue
        vg_pvs.setdefault(parts[1], []).append(parts[0])
    # 已知池映射: VG -> (显示名, 挂载点), 避免未挂载池显示为 mdN
    known_by_vg = {}
    for kname, det in load_state().get("known", {}).items():
        if det.get("type") == "lvm" and det.get("vg") and det.get("mnt"):
            known_by_vg[det["vg"]] = (kname, det["mnt"], det.get("opts", ""))
    mounts = read_mounts()
    arrays = md_arrays()
    for vg, pvs in sorted(vg_pvs.items()):
        md_devs = [p for p in pvs if re.match(r"^/dev/md\d+$", p)]
        if not md_devs:
            continue  # 只管理 mdraid 为底的 LVM 存储池
        md_name = base_disk(md_devs[0])
        info = arrays.get(md_name, {"active": False, "members": []})
        member_disks = {base_disk(x) for x in info["members"]}
        if member_disks & excluded or md_name in excluded:
            continue
        rc2, lvout, _ = sh(["lvs", "--noheadings", "--separator=|", "-o", "lv_name,lv_attr", vg])
        lvs = []
        for ln2 in lvout.splitlines():
            parts2 = [x.strip() for x in ln2.split("|")]
            if parts2 and parts2[0]:
                lvs.append({"name": parts2[0], "attr": parts2[1] if len(parts2) > 1 else ""})
        if not lvs:
            continue
        lv = lvs[0]
        dev = dm_path(vg, lv["name"])
        lv_active = len(lv["attr"]) > 4 and lv["attr"][4:5] == "a"
        mnt, opts, fs = "", "", ""
        cur_mounted = False
        for src, mp, fst, opt in mounts:
            if src == dev and not mp.startswith("/vol00"):
                mnt, opts, fs = mp, opt, fst
                cur_mounted = True
                break
        if mnt:
            name = os.path.basename(mnt)
        elif vg in known_by_vg:
            # 未挂载但已知的池: 沿用原名/原挂载点(仅用于展示), 但不等于已挂载
            name, mnt, opts = known_by_vg[vg]
        else:
            name = md_name
        pools.append({
            "type": "lvm", "name": name, "vg": vg, "md": "/dev/" + md_name,
            "md_name": md_name, "members": info["members"],
            "member_disks": sorted(member_disks), "lv": lv["name"], "dev": dev,
            "mnt": mnt, "opts": opts, "fs": fs,
            "md_active": info["active"], "lv_active": lv_active, "mounted": cur_mounted,
        })
    return pools

# ---------------------------------------------------------------- ZFS pools

def zfs_pools(excluded):
    pools = []
    rc, out, _ = sh(["zpool", "list", "-H", "-o", "name"])
    imported = {ln.strip() for ln in out.splitlines()} if rc == 0 else set()
    for pn in imported:
        rc2, vout, _ = sh(["zpool", "status", "-L", "-v", pn])
        disks = set()
        for ln in vout.splitlines():
            toks = ln.split()
            if toks and re.match(r"^(sd[a-z]+|nvme\d+n\d+)(p?\d+)?$", toks[0]):
                disks.add(base_disk(toks[0]))
        if disks & excluded:
            continue
        datasets = []
        rc3, zout, _ = sh(["zfs", "list", "-H", "-r", "-o", "name,mountpoint", pn])
        if rc3 == 0:
            for ln in zout.splitlines():
                parts = ln.split("\t")
                if len(parts) == 2 and parts[1] != "-" and not parts[1].startswith("/vol00"):
                    datasets.append({"ds": parts[0], "mnt": parts[1]})
        mnt = ""
        for d in datasets:
            if re.match(r"^/vol\d+$", d["mnt"]):
                mnt = d["mnt"]
                break
        pools.append({
            "type": "zfs", "name": os.path.basename(mnt) if mnt else pn, "pool": pn,
            "member_disks": sorted(disks), "datasets": datasets, "mnt": mnt,
            "mounted": any(os.path.ismount(d["mnt"]) for d in datasets) if datasets else False,
        })
    pools.extend(_parse_exported(excluded))
    return pools


def _parse_exported(excluded):
    """解析 `zpool import` 输出, 返回已导出(未导入)的 ZFS 池"""
    rc, out, _ = sh(["zpool", "import"])
    if not out.strip():
        return []
    # 已知池映射: pool名 -> 显示名/挂载点
    known_by_pool = {}
    for kname, det in load_state().get("known", {}).items():
        if det.get("type") == "zfs" and det.get("pool"):
            known_by_pool[det["pool"]] = (kname, det.get("mnt", ""), det.get("datasets", []))
    pools, cur = [], None
    for ln in out.splitlines():
        m = re.match(r"\s+pool:\s+(\S+)", ln)
        if m:
            if cur is not None:
                pools.append(cur)
            cur = {"type": "zfs", "name": m.group(1), "pool": m.group(1),
                   "member_disks": set(), "datasets": [], "mnt": "", "mounted": False}
            continue
        if cur is None:
            continue
        toks = ln.split()
        if toks and re.match(r"^(sd[a-z]+|nvme\d+n\d+)(p?\d+)?$", toks[0]):
            cur["member_disks"].add(base_disk(toks[0]))
    if cur is not None:
        pools.append(cur)
    result = []
    for p in pools:
        if p["member_disks"] & excluded:
            continue
        p["member_disks"] = sorted(p["member_disks"])
        if p["pool"] in known_by_pool:
            kname, kmnt, kds = known_by_pool[p["pool"]]
            p["name"] = kname
            p["mnt"] = kmnt
            p["datasets"] = kds
        result.append(p)
    return result

# ---------------------------------------------------------------- blockers

def _proc_info(pid):
    cmdline, user = "", ""
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            cmdline = f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        pass
    try:
        import pwd  # noqa: PLC0415
        user = pwd.getpwuid(os.stat("/proc/%d" % pid).st_uid).pw_name
    except Exception:  # noqa: BLE001
        pass
    return {"pid": pid, "user": user, "cmd": cmdline[:160] or "(unknown)"}


def blockers_for(mnt, exclude_pids=()):
    """扫描 /proc 找出占用挂载点的进程 (fd/cwd/root/exe)"""
    found = {}
    if not mnt or not os.path.isdir(mnt):
        return found
    me = os.getpid()
    prefix = mnt.rstrip("/") + "/"
    for pid_dir in os.listdir("/proc"):
        if not pid_dir.isdigit():
            continue
        pid = int(pid_dir)
        if pid == me or pid in exclude_pids:
            continue
        paths = []
        fd_dir = "/proc/%d/fd" % pid
        try:
            for fd in os.listdir(fd_dir):
                try:
                    paths.append(os.readlink(os.path.join(fd_dir, fd)))
                except OSError:
                    pass
        except OSError:
            pass
        for attr in ("cwd", "root", "exe"):
            try:
                paths.append(os.readlink("/proc/%d/%s" % (pid, attr)))
            except OSError:
                pass
        if any(p == mnt or p.startswith(prefix) for p in paths):
            found[pid] = _proc_info(pid)
    return found


def file_monitor_pids():
    pids = set()
    for pid_dir in os.listdir("/proc"):
        if not pid_dir.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % pid_dir, "rb") as f:
                if b"trim_file_monitor" in f.read():
                    pids.add(int(pid_dir))
        except OSError:
            pass
    return pids

# ---------------------------------------------------------------- state

def load_state():
    return load_json(STATE_PATH, {"pools": {}, "known": {}})


def save_state(st):
    save_json(STATE_PATH, st)


# ---------------------------------------------------------------- settings

def get_setting(key, default=None):
    cfg = load_json(CONFIG_PATH, {})
    return cfg.get(key, default)


def set_setting(key, value):
    cfg = load_json(CONFIG_PATH, {})
    cfg[key] = value
    save_json(CONFIG_PATH, cfg)
    return cfg


def set_desired(name, desired, details=None):
    with _state_lock:
        st = load_state()
        entry = st["pools"].get(name, {})
        entry["desired"] = desired
        entry["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if details is not None:
            entry["details"] = details
        st["pools"][name] = entry
        save_state(st)


def record_known(pools):
    """把当前发现的池详情持久化, 阵列停止后也能找回并重新挂载"""
    with _state_lock:
        st = load_state()
        known = st.setdefault("known", {})
        for p in pools:
            known[p["name"]] = {
                "type": p["type"], "vg": p.get("vg", ""), "md": p.get("md", ""),
                "md_name": p.get("md_name", ""), "members": p.get("members", []),
                "lv": p.get("lv", ""), "dev": p.get("dev", ""), "mnt": p.get("mnt", ""),
                "opts": p.get("opts", ""), "pool": p.get("pool", ""),
                "datasets": p.get("datasets", []),
            }
        save_state(st)

# ---------------------------------------------------------------- process stop / restore

def unit_of_pid(pid):
    """进程的 systemd 单元名(仅 system.slice 下的托管单元)"""
    try:
        with open("/proc/%d/cgroup" % pid) as f:
            for ln in f.read().splitlines():
                if "/user.slice/" in ln or "/init.scope/" in ln:
                    continue
                m = re.search(r"/([a-zA-Z0-9_.@\\-]+\.(service|socket|target))$", ln)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return None


def _kill_pid(pid, info, killed):
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
        oplog("强制停止: %s (PID %d) 已发送 SIGTERM" % (info["cmd"], pid))
        for _ in range(10):
            time.sleep(0.5)
            try:
                os.kill(pid, 0)
            except OSError:
                break
        try:
            os.kill(pid, 0)
            os.kill(pid, signal.SIGKILL)
            oplog("强制停止: %s (PID %d) 已发送 SIGKILL" % (info["cmd"], pid))
        except OSError:
            pass
        killed.append("PID %d %s" % (pid, info["cmd"]))
    except OSError as e:
        oplog("强制停止 PID %d 失败: %s" % (pid, e))


def stop_blockers(blk):
    """停止占用进程: systemd 托管单元用 systemctl stop(可恢复),
    其他进程 SIGTERM 等待后 SIGKILL。返回 (需恢复的单元列表, 已终止进程说明)"""
    units, killed = [], []
    for pid, info in sorted(blk.items()):
        unit = unit_of_pid(pid)
        if unit:
            rc, out, err = sh(["systemctl", "stop", unit], timeout=60)
            oplog("强制停止: %s (PID %d) -> systemctl stop %s rc=%d %s"
                  % (info["cmd"], pid, unit, rc, (err or out).strip()))
            if rc == 0:
                units.append(unit)
            else:
                _kill_pid(pid, info, killed)
        else:
            _kill_pid(pid, info, killed)
    return units, killed


def restore_units(units):
    for unit in units:
        rc, out, err = sh(["systemctl", "start", unit], timeout=60)
        oplog("恢复服务: systemctl start %s -> rc=%d %s" % (unit, rc, (err or out).strip()))


def start_file_monitor():
    sh(["systemctl", "start", "trim_file_monitor.service"])


# ---------------------------------------------------------------- mdadm 开机组装抑制

def _read_mdadm_conf():
    try:
        with open(MDADM_CONF) as f:
            return f.read().splitlines()
    except OSError:
        return ["#DEVICE partitions containers"]


def _write_mdadm_conf(lines):
    os.makedirs(os.path.dirname(MDADM_CONF), exist_ok=True)
    tmp = MDADM_CONF + ".dm.tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, MDADM_CONF)


def mdadm_ignore_uuids():
    """当前被抑制(开机不组装)的阵列UUID列表"""
    uuids, inside = [], False
    for ln in _read_mdadm_conf():
        s = ln.strip()
        if s == MDADM_BEGIN:
            inside = True
            continue
        if s == MDADM_END:
            inside = False
            continue
        if inside:
            m = re.match(r"ARRAY\s+<ignore>\s+UUID=(\S+)", s)
            if m:
                uuids.append(m.group(1))
    return uuids


def mdadm_ignore_set(uuids):
    """重写管理块: 仅这些UUID被抑制, 配置文件其余内容保留"""
    lines = _read_mdadm_conf()
    out, inside = [], False
    for ln in lines:
        s = ln.strip()
        if s == MDADM_BEGIN:
            inside = True
            continue
        if s == MDADM_END:
            inside = False
            continue
        if inside:
            continue
        out.append(ln)
    uuids = sorted({u for u in uuids if u})
    if uuids:
        out.append(MDADM_BEGIN)
        out.extend("ARRAY <ignore> UUID=%s" % u for u in uuids)
        out.append(MDADM_END)
    _write_mdadm_conf(out)
    oplog("mdadm 开机组装抑制列表已更新: %s" % (", ".join(uuids) if uuids else "(空)"))


def mdadm_ignore_clear():
    mdadm_ignore_set([])


def array_uuid_of(p):
    """取阵列UUID: 先 mdadm --detail(阵列活动), 失败则从成员盘 examine"""
    md = p.get("md", "")
    if md:
        rc, out, _ = sh(["mdadm", "--detail", md])
        if rc == 0:
            m = re.search(r"Array UUID\s*:\s*(\S+)", out)
            if m:
                return m.group(1)
    for member in p.get("members", []):
        rc, out, _ = sh(["mdadm", "--examine", "/dev/" + member])
        if rc == 0:
            m = re.search(r"Array UUID\s*:\s*(\S+)", out)
            if m:
                return m.group(1)
    return None


def sync_ignore_list():
    """按期望状态同步抑制列表: 期望卸载的LVM池 -> 开机不组装; 期望挂载 -> 移除"""
    st = load_state()
    live = {p["name"]: p for p in discover()}
    uuids = []
    for name, ent in st["pools"].items():
        if ent.get("desired") != "unmounted":
            continue
        det = ent.get("details") or st.get("known", {}).get(name, {})
        if det.get("type") != "lvm":
            continue
        # 当前仍挂载的池不加入抑制(可能正在挂载中或刚被用户挂上)
        lp = live.get(name)
        if lp is not None and lp.get("mounted"):
            continue
        u = det.get("array_uuid") or array_uuid_of(det)
        if u:
            uuids.append(u)
            if "array_uuid" not in (ent.get("details") or {}):
                ent.setdefault("details", det)["array_uuid"] = u
    if sorted(set(uuids)) != sorted(set(mdadm_ignore_uuids())):
        with _state_lock:
            save_state(st)
        mdadm_ignore_set(uuids)
    return uuids


def is_safe_service(info):
    """reconcile 自动模式: 仅允许停止 trim 系统服务"""
    unit = unit_of_pid(info["pid"]) or ""
    if unit.startswith("trim_") or unit in SAFE_SERVICE_UNITS:
        return True
    return info["cmd"].startswith("/usr/trim/bin/")

# ---------------------------------------------------------------- pool lookup / discovery

def discover(excluded=None):
    if excluded is None:
        excluded = system_disks()
    return lvm_pools(excluded) + zfs_pools(excluded)


def find_pool(name):
    pools = discover()
    for p in pools:
        if p["name"] == name:
            return p
    st = load_state()
    det = dict(st.get("known", {}).get(name, {}))
    ent = st.get("pools", {}).get(name, {})
    if ent.get("details"):
        det.update(ent["details"])  # 卸载时记录的 details 优先(含array_uuid等新信息)
    if det:
        p = det
        p["name"] = name
        p["mounted"] = False
        p.setdefault("member_disks", [])
        p.setdefault("md_active", False)
        p.setdefault("lv_active", False)
        return p
    return None


def live_md_for_members(members):
    """从 /proc/mdstat 找包含指定成员盘的活动阵列设备(磁盘换位后记录名可能过期)"""
    if not members:
        return None
    for mdname, info in md_arrays().items():
        if info["active"] and set(info["members"]) & set(members):
            return "/dev/" + mdname
    return None


def ensure_md_node(dev):
    """确保 /dev/mdN 设备节点存在(udev偶发不创建)"""
    if not dev or os.path.exists(dev):
        return bool(dev)
    m = re.match(r"^/dev/md(\d+)$", dev)
    if not m:
        return False
    try:
        os.mknod(dev, 0o600 | 0o060000, os.makedev(9, int(m.group(1))))
        oplog("已创建设备节点 %s" % dev)
        return True
    except OSError as e:
        oplog("创建设备节点 %s 失败: %s" % (dev, e))
        return False


def pool_status(p):
    if p.get("mounted"):
        return "mounted"
    if p["type"] == "lvm" and not p.get("md_active"):
        return "stopped"
    return "unmounted"


def list_pools():
    pools = discover()
    record_known(pools)
    st = load_state()
    items, seen = [], set()
    for p in pools:
        ent = st["pools"].get(p["name"], {})
        cur = pool_status(p)
        usage = None
        if p.get("mounted") and os.path.isdir(p.get("mnt", "/nonexist")):
            try:
                sv = os.statvfs(p["mnt"])
                total = sv.f_blocks * sv.f_frsize
                free = sv.f_bavail * sv.f_frsize
                usage = {"total": total, "used": total - free, "free": free,
                         "pct": round((total - free) / total * 100, 1) if total else 0}
            except OSError:
                pass
        items.append({
            "name": p["name"], "type": p["type"],
            "fs": p.get("fs") or ("zfs" if p["type"] == "zfs" else ""),
            "mnt": p.get("mnt", ""), "dev": p.get("dev", p.get("pool", "")),
            "md": p.get("md", ""), "vg": p.get("vg", ""), "pool": p.get("pool", ""),
            "member_disks": sorted(p.get("member_disks", [])),
            "disk_sizes": {d: disk_size(d) for d in sorted(p.get("member_disks", []))},
            "current": cur, "desired": ent.get("desired", "mounted"), "usage": usage,
        })
        seen.add(p["name"])
    # 已知但当前不可见(阵列已停止/已导出)的池; 同时把历史 details 合并进 known
    with _state_lock:
        st2 = load_state()
        changed = False
        for name, ent in st2["pools"].items():
            det = ent.get("details")
            if det and name not in seen and name not in st2["known"]:
                st2["known"][name] = det
                changed = True
        if changed:
            save_state(st2)
        st = st2
    for name, det in st.get("known", {}).items():
        if name in seen or not usable_known(det):
            continue
        ent = st["pools"].get(name, {})
        items.append({
            "name": name, "type": det.get("type", "lvm"),
            "fs": "zfs" if det.get("type") == "zfs" else "",
            "mnt": det.get("mnt", ""), "dev": det.get("dev", det.get("pool", "")),
            "md": det.get("md", ""), "vg": det.get("vg", ""), "pool": det.get("pool", ""),
            "member_disks": [], "disk_sizes": {},
            "current": "stopped", "desired": ent.get("desired", "mounted"), "usage": None,
        })
    return items

# ---------------------------------------------------------------- mount

def do_mount(name):
    p = find_pool(name)
    if not p:
        return False, "未找到存储池 %s" % name
    mounts = {m for _s, m, _f, _o in read_mounts()}
    if p.get("mounted") or (p.get("mnt") and p["mnt"] in mounts):
        set_desired(name, "mounted")
        return True, "已经处于挂载状态"
    if p["type"] == "zfs":
        return mount_zfs(name, p)
    return mount_lvm(name, p)


def mount_lvm(name, p):
    # 挂载前先唤醒成员硬盘, 避免阵列因盘未就绪而被误判降级
    wake_members(p.get("member_disks", []))
    md = p.get("md", "")
    md_name = p.get("md_name", base_disk(md))
    # 移除开机抑制, 允许组装
    arr_uuid = p.get("array_uuid") or array_uuid_of(p)
    if arr_uuid:
        cur = mdadm_ignore_uuids()
        if arr_uuid in cur:
            mdadm_ignore_set([u for u in cur if u != arr_uuid])
    info = md_arrays().get(md_name)
    if not info or not info["active"]:
        members = p.get("members") or []
        if members:
            oplog("组装阵列 %s: %s" % (md, " ".join(members)))
            rc, out, err = sh(["mdadm", "--assemble", "--run", md] + ["/dev/" + m for m in members])
            if rc != 0:
                rc, out, err = sh(["mdadm", "--assemble", "--run", "--scan"])
        else:
            oplog("组装阵列 %s (scan 模式)" % md)
            rc, out, err = sh(["mdadm", "--assemble", "--run", "--scan"])
        if not md_arrays().get(md_name, {}).get("active"):
            return False, "激活 mdadm 阵列失败: %s" % ((err or out).strip() or "未知错误")
    rc, out, err = sh(["vgchange", "-ay", p["vg"]])
    oplog("vgchange -ay %s -> rc=%d %s" % (p["vg"], rc, (err or out).strip()))
    mnt = p.get("mnt") or ""
    if not re.match(r"^/vol\d+$", mnt):
        return False, "无法确定挂载点(%s), 为安全起见拒绝挂载" % (mnt or "空")
    os.makedirs(mnt, exist_ok=True)
    dev = p.get("dev") or ""
    if not os.path.exists(dev):
        return False, "设备 %s 不存在" % dev
    opts = p.get("opts") or "rw,relatime"
    rc, out, err = sh(["mount", "-o", opts, dev, mnt])
    if rc != 0:
        msg = "挂载失败: %s" % (err or out).strip()
        oplog(msg)
        return False, msg
    oplog("已挂载 %s (%s) 到 %s, 选项: %s" % (name, dev, mnt, opts))
    set_desired(name, "mounted")
    start_file_monitor()  # 让 fnOS 文件监控接管新挂载的卷
    return True, "挂载成功"


def kill_file_monitor():
    """杀掉文件监控进程释放卷句柄(系统会自动重启, 且不会再持有已卸载的卷)"""
    import signal
    for pid in file_monitor_pids():
        try:
            os.kill(pid, signal.SIGKILL)
            oplog("已终止文件监控进程 PID %d 以释放卷句柄" % pid)
        except OSError:
            pass


def mount_zfs(name, p):
    # 挂载前先唤醒成员硬盘, 避免池导入时盘未就绪
    wake_members(p.get("member_disks", []))
    pn = p.get("pool", "")
    if not pn:
        return False, "缺少 ZFS 池名"
    rc, out, err = sh(["zpool", "import", pn], timeout=180)
    if rc != 0:
        txt = (err or out).strip()
        low = txt.lower()
        # "already imported" / "already exists" 均表示池已导入, 继续执行挂载步骤
        if "already imported" not in low and "already exists" not in low:
            msg = "zpool import 失败: %s" % txt
            oplog(msg)
            return False, msg
        oplog("ZFS 池 %s 已在导入状态, 直接挂载数据集" % pn)
    # import 对"已导入但数据集被卸载"的池不会重新挂载, 需显式 zfs mount
    for d in p.get("datasets", []):
        mnt, ds = d.get("mnt", ""), d.get("ds", "")
        if mnt and ds and not os.path.ismount(mnt):
            rc2, o2, e2 = sh(["zfs", "mount", ds])
            oplog("zfs mount %s -> rc=%d %s" % (ds, rc2, (e2 or o2).strip()))
    mnt = p.get("mnt", "")
    if mnt and not os.path.ismount(mnt):
        rc3, o3, e3 = sh(["zfs", "mount", "-a"])
        oplog("zfs mount -a -> rc=%d %s" % (rc3, (e3 or o3).strip()))
    if mnt and not os.path.ismount(mnt):
        msg = "ZFS 池已导入但数据集未能挂载: %s" % mnt
        oplog(msg)
        return False, msg
    oplog("ZFS 池 %s 已导入并挂载" % name)
    set_desired(name, "mounted")
    start_file_monitor()
    return True, "挂载成功"


def usable_known(det):
    """known 条目是否可用于挂载(必须有确定的挂载点或 ZFS 池名)"""
    if det.get("type") == "zfs":
        return bool(det.get("pool"))
    return bool(re.match(r"^/vol\d+$", det.get("mnt") or ""))


def do_mount_all():
    """一键恢复挂载: 把所有当前未挂载的存储池恢复挂载(期望状态改为 mounted)"""
    pools = discover()
    record_known(pools)
    st = load_state()
    names = {p["name"] for p in pools}
    # 补充已知但当前不可见(阵列已停止/已导出)的池
    for name, det in st.get("known", {}).items():
        if usable_known(det):
            names.add(name)
    for name, ent in st["pools"].items():
        det = ent.get("details")
        if det and usable_known(det):
            names.add(name)
    results = []
    for name in sorted(names):
        p = find_pool(name)
        if not p or pool_status(p) == "mounted":
            continue
        ok, msg = do_mount(name)
        results.append((name, ok, msg))
        oplog("[恢复挂载] %s -> %s %s" % (name, "成功" if ok else "失败", msg))
    if not results:
        return True, "所有存储池均已处于挂载状态", []
    ok_all = all(r[1] for r in results)
    summary = "、".join("%s%s" % (n, "成功" if ok else "失败") for n, ok, _ in results)
    return ok_all, "恢复挂载完成: " + summary, results

# ---------------------------------------------------------------- unmount

def do_unmount(name, force=False):
    p = find_pool(name)
    if not p:
        return False, "未找到存储池 %s" % name
    if p["type"] == "zfs":
        return unmount_zfs(name, p, force)
    return unmount_lvm(name, p, force)


def _refuse_blockers(other):
    return False, "存在占用进程, 拒绝卸载:\n" + "\n".join(
        "PID %d [%s] %s" % (v["pid"], v["user"] or "?", v["cmd"]) for v in other.values())


def unmount_lvm(name, p, force=False):
    mnt = p.get("mnt") or ""
    dev = p.get("dev", "")
    if not mnt:
        for src, mp, _f, opt in read_mounts():
            if src == dev and not mp.startswith("/vol00"):
                mnt, _o = mp, opt
                break
    details = dict(p)
    if not mnt or not os.path.ismount(mnt):
        # 卷已卸载: 若阵列/卷组仍活动(如被系统重新组装), 继续停用以保持硬盘静默
        vg, md = p.get("vg", ""), p.get("md", "")
        if vg or (md and p.get("md_active")):
            oplog("%s 卷已卸载, 检查并停用残余阵列/卷组" % name)
            sh(["sync"])
            if vg:
                for i in range(4):
                    rc, o, e = sh(["vgchange", "-an", vg])
                    if rc == 0:
                        break
                    time.sleep(1)
                oplog("vgchange -an %s -> rc=%d %s" % (vg, rc, (e or o).strip()))
            md = live_md_for_members(p.get("members", [])) or md
            if md:
                ensure_md_node(md)
                for i in range(4):
                    rc, o, e = sh(["mdadm", "--stop", md])
                    if rc == 0:
                        break
                    time.sleep(1)
                oplog("mdadm --stop %s -> rc=%d %s" % (md, rc, (e or o).strip()))
        # 记录阵列UUID(已卸载分支也要记, 防止被覆盖丢失)
        arr_uuid2 = array_uuid_of(p)
        if arr_uuid2:
            details["array_uuid"] = arr_uuid2
        # 阵列/卷组已停止 -> 安全停转成员硬盘(默认开启, 可设置关闭)
        spin_down_members(p.get("member_disks", []))
        set_desired(name, "unmounted", details)
        return True, "已经处于卸载状态"
    # ---- 占用检查: 除文件监控外的任何占用, 非强制模式一律拒绝 ----
    fm_pids = file_monitor_pids()
    blk = blockers_for(mnt)
    other = {k: v for k, v in blk.items() if k not in fm_pids}
    if other and not force:
        lst = "; ".join("%d(%s) %s" % (v["pid"], v["user"] or "?", v["cmd"]) for v in other.values())
        oplog("拒绝卸载 %s: 存在占用进程 %s" % (mnt, lst))
        return _refuse_blockers(other)
    stopped_units, killed = [], []
    if other and force:
        oplog("强制卸载 %s: 按用户要求停止占用进程 %s" % (
            name, ", ".join("%d %s" % (k, v["cmd"]) for k, v in other.items())))
        stopped_units, killed = stop_blockers(other)
        sh(["sync"])
    # ---- 安全卸载核心流程 ----
    oplog("开始安全卸载 %s (%s @ %s)" % (name, dev, mnt))
    sh(["sync"])
    rc, out, err = sh(["umount", "-l", mnt], timeout=120)
    if rc != 0:
        rc, out, err = sh(["umount", mnt], timeout=120)
    if rc != 0:
        blk2 = {k: v for k, v in blockers_for(mnt).items()
                if k not in fm_pids and k != os.getpid()}
        if blk2 and force:
            s2, k2 = stop_blockers(blk2)
            stopped_units += s2
            killed += k2
            sh(["sync"])
            rc, out, err = sh(["umount", "-l", mnt], timeout=120)
        if rc != 0:
            if blk2:
                return _refuse_blockers(blk2)
            msg = "卸载失败: %s" % (err or out).strip()
            oplog(msg)
            return False, msg
    oplog("umount %s 成功" % mnt)
    # 记录阵列UUID并写入开机抑制: 重启时该硬盘不再被自动组装(不启动)
    arr_uuid = array_uuid_of(p)
    if arr_uuid:
        details["array_uuid"] = arr_uuid
        cur = mdadm_ignore_uuids()
        if arr_uuid not in cur:
            mdadm_ignore_set(cur + [arr_uuid])
    # 停用 VG / 阵列 (带重试, 等待文件系统引用释放)
    vg = p.get("vg", "")
    if vg:
        # 文件监控(fanotify)仍持有卷句柄, 会令 LV 处于 in use; 先终止再停用
        kill_file_monitor()
        for i in range(8):
            rc, o, e = sh(["vgchange", "-an", vg])
            if rc == 0:
                break
            kill_file_monitor()
            time.sleep(1)
        oplog("vgchange -an %s -> rc=%d %s" % (vg, rc, (e or o).strip()))
        if rc != 0:
            restore_units(stopped_units)
            return False, "停用卷组失败: %s" % (e or o).strip()
    # 用实时md设备名(磁盘换位后记录名可能过期), 并确保节点存在
    md = live_md_for_members(p.get("members", [])) or p.get("md", "")
    if md:
        ensure_md_node(md)
        for i in range(6):
            rc, o, e = sh(["mdadm", "--stop", md])
            if rc == 0:
                break
            time.sleep(1)
        oplog("mdadm --stop %s -> rc=%d %s" % (md, rc, (e or o).strip()))
        if rc != 0:
            restore_units(stopped_units)
            return False, "停止 RAID 阵列失败: %s" % (e or o).strip()
    sh(["sync"])
    # 阵列/卷组已停止 -> 安全停转成员硬盘(默认开启, 可设置关闭)
    spin_down_members(p.get("member_disks", []))
    set_desired(name, "unmounted", details)
    oplog("%s 安全卸载完成, 硬盘已静默, 可安全断电拔出" % name)
    restore_units(stopped_units)
    msg = "安全卸载完成, 硬盘已静默"
    if killed:
        msg += "\n已强制终止进程: " + "; ".join(killed)
    if stopped_units:
        msg += "\n已停止服务(已自动重启): " + ", ".join(stopped_units)
    return True, msg


def unmount_zfs(name, p, force=False):
    pn = p.get("pool", "")
    datasets = p.get("datasets") or []
    details = dict(p)
    mnts = [d["mnt"] for d in datasets if os.path.ismount(d["mnt"])]
    if not mnts:
        # 数据集未挂载: 若池仍未导出则尝试导出, 已导出则直接记录状态
        rc, out, err = sh(["zpool", "list", "-H", "-o", "name"])
        still_imported = pn and pn in out.split() if rc == 0 else False
        if still_imported:
            # 文件监控进程持有卷句柄会导致 pool is busy, 先终止(会自动重启)
            kill_file_monitor()
            for i in range(8):
                rc2, o2, e2 = sh(["zpool", "export", pn], timeout=180)
                if rc2 == 0:
                    break
                time.sleep(2)
            if rc2 != 0:
                msg = "zpool export 失败: %s" % (e2 or o2).strip()
                oplog(msg)
                return False, msg
            oplog("ZFS 池 %s 已导出" % name)
        # 池已导出 -> 安全停转成员硬盘
        spin_down_members(p.get("member_disks", []))
        set_desired(name, "unmounted", details)
        return True, "已经处于卸载状态" if not still_imported else "安全卸载完成, 硬盘已静默"
    fm_pids = file_monitor_pids()
    other = {}
    for m in mnts:
        for k, v in blockers_for(m).items():
            if k not in fm_pids and k != os.getpid():
                other[k] = v
    if other and not force:
        oplog("拒绝卸载 ZFS %s: 存在占用进程" % name)
        return _refuse_blockers(other)
    stopped_units, killed = [], []
    if other and force:
        oplog("强制卸载 ZFS %s: 按用户要求停止占用进程 %s" % (
            name, ", ".join("%d %s" % (k, v["cmd"]) for k, v in other.items())))
        stopped_units, killed = stop_blockers(other)
        sh(["sync"])
    oplog("开始安全卸载 ZFS 池 %s (%s)" % (name, pn))
    sh(["sync"])
    for m in mnts:
        rc, out, err = sh(["umount", "-l", m], timeout=120)
        if rc != 0:
            rc, out, err = sh(["umount", m], timeout=120)
        oplog("umount %s -> rc=%d %s" % (m, rc, (err or out).strip()))
        if rc != 0:
            restore_units(stopped_units)
            return False, "卸载 %s 失败: %s" % (m, (err or out).strip())
    # 文件监控进程持有卷句柄会导致 pool is busy, 先终止(会自动重启)
    kill_file_monitor()
    for i in range(8):
        rc, out, err = sh(["zpool", "export", pn], timeout=180)
        if rc == 0:
            break
        # pool is busy 常为瞬态(其他命名空间句柄尚未释放), 等待重试
        time.sleep(2)
    if rc != 0:
        msg = "zpool export 失败: %s" % (err or out).strip()
        oplog(msg)
        restore_units(stopped_units)
        return False, msg
    sh(["sync"])
    # 池已导出 -> 安全停转成员硬盘
    spin_down_members(p.get("member_disks", []))
    set_desired(name, "unmounted", details)
    oplog("ZFS 池 %s 安全卸载完成, 硬盘已静默" % name)
    restore_units(stopped_units)
    msg = "安全卸载完成, 硬盘已静默"
    if killed:
        msg += "\n已强制终止进程: " + "; ".join(killed)
    if stopped_units:
        msg += "\n已停止服务(已自动重启): " + ", ".join(stopped_units)
    return True, msg

# ---------------------------------------------------------------- reconcile

def reconcile_once():
    pools = discover()
    record_known(pools)
    by_name = {p["name"]: p for p in pools}
    with _op_lock:  # 与挂载/卸载操作互斥, 避免竞争; 状态必须在锁内读取
        try:
            sync_ignore_list()
        except Exception as e:  # noqa: BLE001
            oplog("[reconcile] 同步开机抑制列表异常: %r" % e)
        st = load_state()
        for name, ent in list(st["pools"].items()):
            desired = ent.get("desired", "mounted")
            p = by_name.get(name)
            if p is None:
                continue
            cur = pool_status(p)
            if desired == "unmounted" and cur in ("mounted", "unmounted"):
                # 卷已卸载但阵列/池可能仍活动(被系统重新组装), 一并处理
                fm_pids = file_monitor_pids()
                mnt = p.get("mnt", "")
                other = {k: v for k, v in blockers_for(mnt).items() if k not in fm_pids} if cur == "mounted" else {}
                if other and not all(is_safe_service(v) for v in other.values()):
                    oplog("[reconcile] %s 存在非系统服务占用, 跳过自动卸载: %s" % (
                        name, ", ".join(v["cmd"] for v in other.values())))
                    continue
                oplog("[reconcile] %s 期望卸载, 当前状态 %s, 自动安全卸载" % (name, cur))
                ok, msg = do_unmount(name, force=True)
                oplog("[reconcile] %s -> %s" % (name, msg))
            elif desired == "mounted" and cur in ("unmounted", "stopped"):
                oplog("[reconcile] %s 期望挂载但当前未挂载, 自动挂载" % name)
                ok, msg = do_mount(name)
                oplog("[reconcile] %s -> %s" % (name, msg))
    # 期望卸载且池已停止: 维持成员盘停转(若被系统触碰后重新自旋, 则再次停转)
    if get_setting("auto_spindown", True):
        for name, ent in list(st["pools"].items()):
            if ent.get("desired") != "unmounted":
                continue
            p = by_name.get(name)
            if p is None:
                continue
            if p.get("md_active") or p.get("lv_active"):
                continue
            if p.get("type") == "zfs" and p.get("mounted"):
                continue
            spin_down_members(p.get("member_disks", []), auto=True)


def reconcile_loop():
    time.sleep(RECONCILE_INITIAL_DELAY)  # 等系统存储服务先完成开机挂载
    while True:
        try:
            reconcile_once()
        except Exception as e:  # noqa: BLE001
            oplog("[reconcile] 异常: %r" % e)
        time.sleep(RECONCILE_INTERVAL)

# ---------------------------------------------------------------- auth

def load_sessions():
    global _sessions
    with _sessions_lock:
        if not _sessions:
            data = load_json(SESSIONS_PATH, {})
            now = time.time()
            _sessions = {k: v for k, v in data.items() if v > now}
    return _sessions


def save_sessions():
    with _sessions_lock:
        save_json(SESSIONS_PATH, _sessions)


def check_password(pw):
    cfg = load_json(CONFIG_PATH, {})
    if not cfg.get("pass_sha256"):
        return False
    h = hashlib.sha256((cfg["pass_salt"] + pw).encode()).hexdigest()
    return secrets.compare_digest(h, cfg["pass_sha256"])


def auth_ok(cookie):
    load_sessions()
    tok = ""
    for part in (cookie or "").split(";"):
        part = part.strip()
        if part.startswith("dm_token="):
            tok = part[9:]
    with _sessions_lock:
        exp = _sessions.get(tok)
    return exp is not None and exp > time.time()

# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "DiskMounter/1.3"

    def log_message(self, fmt, *args):  # 静默访问日志
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _cookie(self):
        return self.headers.get("Cookie", "")

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            try:
                with open(INDEX_PATH, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(404, {"error": "index.html missing"})
            return
        if not auth_ok(self._cookie()):
            self._send(401, {"error": "unauthorized"})
            return
        if path == "/api/pools":
            self._send(200, {"pools": list_pools(), "ts": time.time()})
            return
        if path == "/api/log":
            self._send(200, {"log": self._tail(OPSLOG_PATH, 200)})
            return
        if path == "/api/disks":
            self._send(200, {
                "disks": scan_disks(),
                "auto_spindown": get_setting("auto_spindown", True),
                "hdparm_ok": (lambda h: h != "hdparm" and os.path.exists(h))(hdparm_bin()),
                "ts": time.time(),
            })
            return
        if path == "/api/settings":
            self._send(200, {"auto_spindown": get_setting("auto_spindown", True)})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        n = int(self.headers.get("Content-Length") or 0)
        body = {}
        if n:
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                body = {}
        if not isinstance(body, dict):
            body = {}
        if path == "/api/login":
            if check_password(str(body.get("password", ""))):
                tok = secrets.token_hex(24)
                with _sessions_lock:
                    _sessions[tok] = time.time() + 7 * 86400
                save_sessions()
                self._send(200, {"ok": True},
                           headers={"Set-Cookie": "dm_token=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=604800" % tok})
            else:
                self._send(403, {"ok": False, "error": "口令错误"})
            return
        if path == "/api/logout":
            tok = ""
            for part in self._cookie().split(";"):
                part = part.strip()
                if part.startswith("dm_token="):
                    tok = part[9:]
            with _sessions_lock:
                _sessions.pop(tok, None)
            save_sessions()
            self._send(200, {"ok": True})
            return
        if not auth_ok(self._cookie()):
            self._send(401, {"error": "unauthorized"})
            return
        if path in ("/api/mount", "/api/unmount"):
            name = str(body.get("name", "")).strip()
            if not name or "/" in name or name.startswith("."):
                self._send(400, {"error": "参数错误"})
                return
            with _op_lock:
                if path == "/api/mount":
                    ok, msg = do_mount(name)
                else:
                    ok, msg = do_unmount(name, force=bool(body.get("force")))
            self._send(200, {"ok": ok, "message": msg})
            return
        if path == "/api/mount-all":
            with _op_lock:
                ok, msg, results = do_mount_all()
            self._send(200, {"ok": ok, "message": msg, "results": results})
            return
        if path == "/api/disk/spindown":
            disk = str(body.get("disk", "")).strip()
            if not _valid_disk(disk):
                self._send(400, {"error": "参数错误"})
                return
            with _op_lock:
                ok, msg = spin_down_disk(disk)
            self._send(200, {"ok": ok, "message": msg})
            return
        if path == "/api/disk/spinup":
            disk = str(body.get("disk", "")).strip()
            if not _valid_disk(disk):
                self._send(400, {"error": "参数错误"})
                return
            with _op_lock:
                ok, msg = spin_up_disk(disk)
            self._send(200, {"ok": ok, "message": msg})
            return
        if path == "/api/disk/resume":
            disk = str(body.get("disk", "")).strip()
            if not _valid_disk(disk):
                self._send(400, {"error": "参数错误"})
                return
            with _op_lock:
                ok, msg = resume_disk(disk)
            self._send(200, {"ok": ok, "message": msg})
            return
        if path == "/api/setting":
            key = str(body.get("key", "")).strip()
            if key not in ("auto_spindown",):
                self._send(400, {"error": "不允许设置的键"})
                return
            val = body.get("value")
            if key == "auto_spindown":
                val = bool(val)
            set_setting(key, val)
            self._send(200, {"ok": True, "value": val})
            return
        self._send(404, {"error": "not found"})

    @staticmethod
    def _tail(path, n):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return "".join(f.readlines()[-n:])
        except OSError:
            return ""

# ---------------------------------------------------------------- CLI / main

def cli_restore_all():
    """卸载/升级前调用: 把所有被本应用卸载的池恢复挂载(尽力而为)"""
    st = load_state()
    pools = discover()
    by_name = {p["name"]: p for p in pools}
    for name, ent in st["pools"].items():
        if ent.get("desired") == "unmounted":
            p = by_name.get(name)
            if p and pool_status(p) == "mounted":
                continue
            ok, msg = do_mount(name)
            oplog("[restore] %s -> %s" % (name, msg))
            if ok:
                set_desired(name, "mounted")
    return 0


def main():
    os.makedirs(VAR_DIR, exist_ok=True)
    if len(sys.argv) > 1:
        if sys.argv[1] == "restore-all":
            sys.exit(cli_restore_all())
        if sys.argv[1] == "clear-ignore":
            # 卸载应用时清除开机抑制, 避免硬盘永远无法被系统组装
            mdadm_ignore_clear()
            sys.exit(0)
        if sys.argv[1] == "spindown-stopped":
            # 手动: 对所有"期望卸载且已停止"的存储池成员盘执行安全停转
            st = load_state()
            pools = discover()
            by_name = {p["name"]: p for p in pools}
            n = 0
            for name, ent in st["pools"].items():
                if ent.get("desired") != "unmounted":
                    continue
                p = by_name.get(name)
                if p and not (p.get("md_active") or p.get("lv_active")) \
                        and not (p.get("type") == "zfs" and p.get("mounted")):
                    spin_down_members(p.get("member_disks", []), auto=False)
                    n += 1
            oplog("[spindown-stopped] 已对 %d 个已停止存储池的成员盘执行停转" % n)
            sys.exit(0)
    threading.Thread(target=reconcile_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    oplog("磁盘挂载管家后端已启动, 端口 %d" % PORT)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
