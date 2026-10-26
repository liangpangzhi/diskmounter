# 磁盘挂载管家（diskmounter）— 飞牛 fnOS 原生 fpk 应用

针对飞牛 NAS（fnOS）定制的存储池挂载/安全卸载图形化管理应用。

## 当前状态

✅ 已在 NAS 上安装并运行（appname: `diskmounter`，端口 5688，root 权限，装于系统分区）。
当前版本 **v1.2.7**（2026-10-26 修复两个缺陷并已热拷贝+重启生效：①存储池实际未挂载却显示"已挂载"、reconcile 不自动重挂；②卸载 LVM 时"停用卷组失败: LV in use"）。
因 fnOS 的 appcenter-cli install-fpk 不替换已安装应用的文件，升级采用「fnpack build + 拷贝二进制 + 重启」方式，见下方"重新构建"。

**当前挂载状态**（2026-10-26 13:25，全部 8 池已恢复挂载）：
- 挂载中：vol2（ZFS）、vol3、vol4、vol5、vol6、vol7、vol8、vol9
- vol3 曾因内核态 mdadm 容器卡死（测试遗留）无法挂载，重启后已恢复
- 如需卸载：应用内点「安全卸载」，重启后期望状态保持（开机后约 3 分钟内自动恢复到期望状态）

## 开机不启动机制（实测结论）

对期望卸载的 LVM 存储池，应用自动在 `/etc/mdadm/mdadm.conf` 写入 `ARRAY <ignore> UUID=xxx`。

**实测结论（重要）**：
- **udev 阶段**：抑制有效——`mdadm --incremental` 会跳过被抑制的阵列 ✓
- **initramfs 阶段**（开机 t≈2.4 秒，最早的阶段）：飞牛的内存盘使用**镜像内嵌的旧 mdadm.conf**，会组装所有阵列，抑制管不到这个阶段 → 硬盘在开机时仍会短暂转动
- **最终效果**：开机后约 2.5~3 分钟（应用自启动 + reconcile），被卸载的池会被自动卸载、阵列停止，硬盘恢复静默；重启后期望状态保持
- 若需彻底"开机不转动"，需 `update-initramfs -u` 重建内存盘使内嵌配置包含抑制列表（有无法启动的风险，未采纳）；且飞牛存储服务运行时仍可能重新组装阵列

其他机制不变：
- **恢复挂载时**：应用先移除对应 ignore 条目，再显式组装
- **卸载应用时**：自动清除整个管理块，硬盘恢复系统默认行为
- **ZFS 池（vol2）例外**：无法阻止开机导入，应用启动后约 10 秒内自动导出

**访问方式：**
- 飞牛桌面 → 「磁盘挂载管家」图标
- 或浏览器直接访问：`http://<你的NAS地址>:5688`
- 访问口令：安装/升级时经 `.env` 的 `dm_password` 设置，之后可在应用配置里修改（升级不会覆盖）

## 功能

- 图形化列出**系统盘以外**的全部存储池（自动排除系统盘 sda、USB DOM sdj 及可移动设备）
- 支持 LVM 池（vol3~vol9，mdadm+btrfs）与 ZFS 池（vol2）
- **挂载/恢复挂载**：
  - 单池「恢复挂载」按钮：自动组装 RAID 阵列 → 激活卷组 → 按原挂载点/原选项挂载（ZFS 为 `zpool import`）
  - 工具栏「⟳ 全部恢复挂载」：一键恢复所有未挂载的存储池（含阵列已停止的池），并把期望状态改为挂载
- **安全卸载**（不影响数据）：
  1. 扫描 /proc 检测占用进程（句柄/工作目录），**有任何占用即拒绝卸载**并列出进程清单
  2. 被拒绝时，弹窗会列出占用进程并提供「⚡ 强制停止占用进程并卸载」按钮：
     - systemd 托管服务（如 share_service、filestor_service、trim_file_monitor）→ `systemctl stop` 优雅停止，卸载完成后自动重启
     - 其他进程 → 先 SIGTERM 等待退出，仍存活则 SIGKILL
     - 强制停止后仍按标准流程执行：sync → umount → 停卷组/阵列
  3. 飞牛自带 `trim_file_monitor` 服务持有每个卷的句柄，普通卸载时也会自动短暂停止它释放句柄，结束后自动重启
  4. 卸载后硬盘完全静默，可安全断电拔出
- **重启保持**：期望状态持久化，后端启动及每 120 秒 reconcile，自动维持"已卸载/已挂载"状态
- 二次确认（需输入池名）、操作日志查看、用量展示

## 卸载本应用时

卸载前脚本会自动把被卸载的池恢复挂载，避免系统状态不一致。

## 文件结构（源码）

```
fn-diskmounter/
├── manifest            # 应用元信息（service_port=5688, install_type=root）
├── ICON.PNG / ICON_256.PNG
├── cmd/                # 生命周期脚本（main 启停、install_callback 写入口令等）
├── config/             # privilege: run-as root
├── wizard/install      # 安装向导（安全须知 + 访问口令）
├── app/bin/diskm.py    # 后端（纯 Python 标准库，适配 NAS python3.11）
├── app/www/index.html  # 前端单页
├── app/ui/             # 桌面图标入口
├── build.sh            # 一键构建: 读 manifest 版本, 产出 diskmounter_<version>.fpk
└── diskmounter_1.2.7.fpk # 可安装包(文件名始终带版本号)
```

## 重新构建 / 修改

NAS 上已保留构建目录 `/root/diskmounter-build/fn-diskmounter`。

**版本号约定（重要）**：manifest 的 `version=` 决定版本；构建产出的 fpk 文件名**始终带版本号** `diskmounter_<version>.fpk`。
流程：本地改代码 → 同步到 NAS 构建目录 → 在构建目录执行 `./build.sh`（自动清理 `__pycache__`/`*.pyc`、`fnpack build`、并按版本号重命名输出）。
安装/升级传参用 `.env` 文件，键名与向导字段一致（如 `dm_password=xxx`，值不要含 `@` 等特殊字符）。

## 注意事项

- 卸载池前请确认下载、Docker 容器、影音库等已停止对该卷读写
- 卸载是文件系统级安全操作，不会删除/格式化数据；重新挂载后一切照旧
- ZFS 池重新挂载依赖 `zpool import`，挂载点由池属性自动恢复
