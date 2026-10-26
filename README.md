# 磁盘挂载管家（diskmounter）— 飞牛 fnOS 原生 fpk 应用

针对飞牛 NAS（192.168.5.11）定制的存储池挂载/安全卸载图形化管理应用。

## 当前状态

✅ 已在 NAS 上安装并运行（appname: `diskmounter`，端口 5688，root 权限，装于系统分区）。
当前版本 **v1.3.0**（2026-10-27 新增「硬盘停转」功能，详见下文）。
历史：v1.2.7（2026-10-26 修复两个缺陷并已热拷贝+重启生效：①存储池实际未挂载却显示"已挂载"、reconcile 不自动重挂；②卸载 LVM 时"停用卷组失败: LV in use"）。
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
- 或浏览器直接访问：`http://192.168.5.11:5688`（注：`:5666` 是飞牛系统登录页端口，本应用端口为 `5688`）
- 访问口令：`123456`（你在应用配置中设置；建议改为更安全的密码，改后升级应用不会覆盖它）

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

## 硬盘停转（v1.3.0 新增 · 绝对安全优先）

> 核心原则：**只在「卸载后、且硬盘 100% 空闲、且不属于任何活跃存储池」时才允许停转**。停转不会删除/修改任何数据；硬盘被访问时会自动自旋启动。

**安全闸门（任一命中即禁止停转，并明确提示原因）：**
1. **系统盘**（根/EFI/boot 所在盘）→ 严禁停转，保护系统；
2. **活跃存储池盘**（仍属于活动 mdadm 阵列 / 活跃 LVM 卷组 / 已导入 ZFS 池的成员盘）→ 禁止停转，否则存储池会降级甚至损坏数据；
3. **已挂载（使用中）的独立盘** → 禁止停转，需先到「存储池管理」安全卸载。

**功能：**
- **自动停转**：在「存储池管理」执行安全卸载后，阵列停止/卷组停用/ZFS 导出的瞬间，自动对其成员硬盘发送停转指令（`hdparm -y`；SAS 盘回退 `sg_start --stop`）。可用面板上的「卸载后自动停转」开关关闭。
- **扫描与手动停转**：「硬盘停转」选项卡一键扫描全部数据盘，按安全等级分类（系统盘·受保护 / 存储池中·活跃 / 已挂载·使用中 / 已导出ZFS池·会被唤醒 / 可停转·空闲）。除系统盘、活跃池盘、已挂载盘外，其余盘均可手动「停转硬盘」（需输入盘名二次确认）。
- **唤醒 / 恢复使用**：对停转的硬盘可「唤醒」（非破坏性读取首扇区触发自旋启动），或「恢复使用」（先唤醒再自动重新挂载其所属存储池）。
- **挂载前自动唤醒**：执行「恢复挂载 / 恢复使用」时会先唤醒成员盘，避免阵列因盘未就绪而被误判为降级。
- **reconcile 维持**：对被「期望卸载」且已停止的存储池，后台每 30 秒维持成员盘停转（若被系统触碰后重新自旋则再次停转）。

**实现要点：**
- 停转指令在执行时**再次**校验安全等级（不只是扫描时），杜绝状态变化带来的风险；
- 设备名严格校验（正则 `^(sd|nvme|hd|vd)[...]$`），命令经参数列表传入（非 shell），无注入风险；
- 仅使用标准 Linux 工具 `hdparm`/`sg_start`，无需额外依赖；缺失 `hdparm` 时面板会给出警告。

**新增 CLI：** `python3 diskm.py spindown-stopped` —— 手动对所有「期望卸载且已停止」的存储池成员盘执行安全停转。

**已知现象 · 已导出 ZFS 池盘停转后很快被唤醒（`pool_exported`）：**
若某块盘属于一个「已导出（未导入）但元数据仍在」的 ZFS 存储池（分区 `TYPE=zfs_member`），飞牛的 `zfs-zed` 守护进程会持续运行 `zpool import` 尝试导入该池；import 扫描会打开该盘读取 ZFS 标签，导致它一旦停转就被立刻唤醒——表现为「停转没反应」。这是系统行为，**并非停转指令失效**（`hdparm -y` 确实生效，只是被后台唤醒）。应用已能识别此类盘并标注为「已导出ZFS池·会被唤醒」，明确提示：如需长期保持静默停转，请在飞牛「存储管理」中删除该 ZFS 存储池（清除其元数据），飞牛便不再持续尝试导入该盘。该分类下仍可手动停转（安全、不伤数据），只是无法长期保持静默。

**访问方式（停转面板）：**
- 飞牛桌面 → 「磁盘挂载管家」图标 → 切到「硬盘停转」选项卡
- 或浏览器直接访问：`http://<NAS_IP>:5688` 后切到「硬盘停转」
- 访问口令同应用配置（默认 `123456`，建议改为更安全的密码）

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

构建在**本地**完成（项目根目录下的 `build_fpk.py`，纯 Python 标准库，无需 fnOS 工具链）：
- 改完 `_unpack/app/` 下的 `bin/diskm.py`、`www/index.html` 后，直接运行 `python3 build_fpk.py`；
- 脚本会：从现有 `diskmounter_1.3.0.fpk` 取出 `cmd/`、`config/`、`wizard/`、`ICON*`、`manifest` → 覆盖进更新后的 `app/` → 重新打包 `app.tgz` → **重算 `checksum = md5(app.tgz)`** 写回 `manifest` → 产出校验通过的 `diskmounter_1.3.0.fpk`；
- 输出即为可直接「应用中心 → 手动安装」或按「方式二」热升级的包。

**版本号约定（重要）**：manifest 的 `version=` 决定版本；构建产出的 fpk 文件名**始终带版本号** `diskmounter_<version>.fpk`。

## 升级到 v1.3.0（新增停转功能）

产出物：`diskmounter_1.3.0.fpk`（位于项目根目录，已重算 `checksum = md5(app.tgz)`，可直接安装）。

**方式一（推荐，干净）：卸载旧版 → 安装新版**
1. 飞牛「应用中心」卸载 `diskmounter`（卸载脚本会自动把已卸载的池恢复挂载，无需担心状态不一致）；
2. 「应用中心 → 手动安装」上传 `diskmounter_1.3.0.fpk` 即可。

**方式二（热升级，不动数据）：仅替换两个改动文件**
1. 将新包内 `app/bin/diskm.py` 与 `app/www/index.html` 覆盖到已安装目录：
   - 实际安装路径：`/usr/local/apps/@appcenter/diskmounter/bin/diskm.py`、`/usr/local/apps/@appcenter/diskmounter/www/index.html`
   - 配置目录：`/usr/local/apps/@appdata/diskmounter`（config.json/sessions.json 在此，升级不覆盖）
2. 重启后端。两种方式任选：
   - **A. 飞牛「应用中心」**：在应用中心对该应用执行「停止」再「启动」，fnOS 会用正确的 `TRIM_*` 环境变量重新拉起；
   - **B. SSH 手动拉起（当应用中心不可用时）**：fnOS **不会**把包内的 `cmd/main` 安装到运行时目录，因此不能用 `cmd/main` 重启；需以 root 直接启动并带齐 `TRIM_*` 环境变量，例如：
     ```bash
     APP=/usr/local/apps/@appcenter/diskmounter
     VAR=/usr/local/apps/@appdata/diskmounter
     pkill -f "bin/diskm.py"; sleep 2; rm -f $VAR/app.pid
     export TRIM_APPDEST=$APP TRIM_PKGVAR=$VAR \
            TRIM_PKGETC=/usr/local/apps/@appconf/diskmounter \
            TRIM_PKGMETA=/vol1/@appmeta/diskmounter \
            TRIM_PKGHOME=/usr/local/apps/@apphome/diskmounter \
            TRIM_PKGTMP=/usr/local/apps/@apptemp/diskmounter \
            TRIM_APPNAME=diskmounter TRIM_SERVICE_PORT=5688 \
            TRIM_RUN_USERNAME=root TRIM_RUN_UID=0
     nohup /usr/bin/python3 $APP/bin/diskm.py >> $VAR/backend.log 2>&1 &
     echo $! > $VAR/app.pid
     ```

> 注：v1.3.0 仅改动 `diskm.py` 与 `index.html`，manifest/cmd/config/ui 结构与端口（5688）不变，两种方式都安全。升级后访问口令、期望状态等配置均予以保留。替换前建议先备份原文件（如 `cp -p .../diskm.py .../diskm.py.bak.$(date +%s)`）。

## 注意事项

- 卸载池前请确认下载、Docker 容器、影音库等已停止对该卷读写
- 卸载是文件系统级安全操作，不会删除/格式化数据；重新挂载后一切照旧
- ZFS 池重新挂载依赖 `zpool import`，挂载点由池属性自动恢复
