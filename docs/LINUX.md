# Linux 使用说明

Linux 版提供独立可执行文件，通过命令行启动和管理，并提供与 Windows 版相同的 WebUI。它不需要安装 Python 或图形桌面，也没有托盘图标。可以把管理器放在一台 Linux 服务器上，让它持续承担端口中转。

## 安装独立程序

系统需要 OpenSSH 客户端，以及一个可写的安装目录。独立程序已在 **Ubuntu 24.04、x86_64** 实测，使用 **glibc 2.39** 基线；其他发行版未作兼容承诺。登录后自动启动还需要服务器支持 `systemd --user`，手动启动不依赖 systemd。

下载 **[JumperManager-Linux-x86_64.tar.gz](https://github.com/pgq18/JumperManager/releases/latest/download/JumperManager-Linux-x86_64.tar.gz)**，在压缩包所在目录执行：

```sh
tar -xzf JumperManager-Linux-x86_64.tar.gz -C "$HOME"
cd ~/JumperManager
ssh -V
chmod +x jumper-manager
```

包内的 `JumperManager/` 包含独立程序及许可证等文件，请完整保留。独立程序内已包含 Python 运行时和 WebUI，运行时无需另外放置 `app.py`、`web/` 或安装 Python 包。已有安装需要更新时，先按后面的升级步骤停止并备份。

发布页同时提供 [SHA256SUMS.txt](https://github.com/pgq18/JumperManager/releases/latest/download/SHA256SUMS.txt)，可用于核对下载包。对应源码和重建说明见 [SOURCE-LINUX.md](../SOURCE-LINUX.md)。

## 配置 SSH

管理器读取**运行它的 Linux 用户**的 `~/.ssh/config` 及其中引用的文件。请先在该用户的终端中确认需要的 SSH 别名可以登录，例如：

```sh
ssh source-device
ssh target-device
```

`source-device` 和 `target-device` 是示例别名，请替换成自己的配置。后台 SSH 连接不能临时输入密码或确认新的主机密钥；首次登录和主机身份核对应在终端完成。使用带口令的密钥时，需要让相应运行环境中的 `ssh-agent` 提供已解锁的密钥。

管理器本身不需要系统安装 Python；通过 SSH 检查的远端 Linux 设备仍需 Python 3，用于端口与监听进程检查。

把管理器从 Windows 移到 Linux，不会自动迁移 SSH 配置、密钥或 agent。WebUI 中的“本机”也随之变成运行管理器的 Linux 设备。

## 启动和停止管理器

在安装目录中运行：

```sh
./jumper-manager start
./jumper-manager status
```

`start` 在后台启动管理器，默认不打开浏览器，WebUI 地址为 `http://127.0.0.1:8765`。使用下面的命令管理它：

| 命令 | 用途 |
| --- | --- |
| `./jumper-manager start` | 后台启动管理器 |
| `./jumper-manager status` | 查看管理器是否运行及界面地址 |
| `./jumper-manager stop` | 停止管理器及它持有的所有映射 |
| `./jumper-manager restart` | 停止后重新启动管理器 |
| `./jumper-manager serve` | 在当前终端前台运行，适合排查启动问题 |
| `./jumper-manager open` | 在有桌面的 Linux 环境中打开管理界面 |

停止管理器会保留映射配置。重新启动管理器时，只有设置了“打开管理器时自动启动此映射”的映射会自动建立，其余需要手动启动。关闭浏览器不会停止后台管理器或映射。

如果默认端口被占用，可以选择其他端口：

```sh
./jumper-manager start --port 8876
```

如果管理器已经运行，先停止它，再更换监听端口。

需要使用另一份 SSH 配置时，在启动时指定路径：

```sh
./jumper-manager start --ssh-config "$HOME/.ssh/manager-config"
```

## 在自己的电脑上访问服务器 WebUI

管理界面只监听服务器的回环地址。服务器有桌面时，可以直接打开 `http://127.0.0.1:8765`；没有桌面时，在**自己的电脑**上执行：

```sh
ssh -N -L 8765:127.0.0.1:8765 manager-server
```

`manager-server` 是自己的电脑上可用的服务器 SSH 别名。保持这条 SSH 连接运行，然后在自己电脑的浏览器打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)。这条转发用于访问管理界面；关闭它不会停止服务器上的管理器及已有映射。

**本地浏览器端口与管理器实际监听端口应一致。** 如果自己电脑的 `8765` 已被占用，在服务器使用 `./jumper-manager start --port 8876`，再在自己的电脑执行：

```sh
ssh -N -L 8876:127.0.0.1:8876 manager-server
```

此时打开 [http://127.0.0.1:8876](http://127.0.0.1:8876)。不要只改 `-L` 左侧的本地端口；管理器会核对请求的地址和来源。

## 通过命令行管理设备和映射

先启动管理器，再执行下列命令。CLI 与 WebUI 管理同一份配置，在一端所做的更改会反映到另一端。

查看全部映射、启动或停止单条映射，可以直接使用短命令：

```sh
./jumper-manager list
./jumper-manager start demo
./jumper-manager stop demo
./jumper-manager list --json
```

`demo` 可替换为完整 ID、唯一 ID 前缀或唯一名称；带空格的名称用引号包住。**`stop demo` 只停止指定映射；`stop` 不带名称时会停止管理器及全部映射。** 同样，`start demo` 启动映射，`start` 启动管理器。短命令等价于 `mappings list`、`mappings start demo`、`mappings stop demo`，原有写法继续可用。

操作单条映射时只附加 `--json`，不要混用管理器的 `--port`、`--ssh-config` 等启动参数。名称或前缀不唯一时会报错，不会改为停止管理器。

查看、测试和同步 SSH 设备：

```sh
./jumper-manager hosts list
./jumper-manager hosts test target-device
./jumper-manager hosts sync
```

创建一条从 `source-device:50052` 转到 `target-device:50051` 的映射：

```sh
./jumper-manager mappings add \
  --name demo \
  --source-host source-device --source-port 50052 \
  --target-host target-device --target-port 50051
./jumper-manager start demo
./jumper-manager mappings show demo
```

将来源设备改成 `--source-host local`，即可从运行管理器的 Linux 设备访问远端服务。目标也可以选择 `--target-host local`。`add` 保存配置，之后的 `start` 才建立隧道。

常用操作：

| 命令 | 用途 |
| --- | --- |
| `./jumper-manager list` | 列出映射、状态及引用标识 |
| `./jumper-manager mappings show demo` | 查看一条映射的详情 |
| `./jumper-manager mappings check demo` | 更新隧道和目标监听进程快照 |
| `./jumper-manager start demo` | 启动单条映射 |
| `./jumper-manager stop demo` | 停止单条映射，保留配置 |
| `./jumper-manager mappings edit demo --target-port 50053` | 修改目标端口；先停止映射 |
| `./jumper-manager mappings delete demo` | 删除映射配置；建议先停止并核对名称 |
| `./jumper-manager mappings pin demo` | 置顶映射 |
| `./jumper-manager mappings unpin demo` | 取消置顶 |

表中的 `demo` 可替换成完整映射 ID、唯一的 ID 前缀，或唯一的映射名称。如果有重名或前缀匹配多条映射，命令会要求使用更明确的标识。名称包含空格时用引号包住。

创建和编辑支持以下参数：

| 参数 | 含义 |
| --- | --- |
| `--name` | 映射名称 |
| `--source-host` / `--source-port` | 来源设备别名和监听端口 |
| `--target-host` / `--target-port` | 目标设备别名和服务端口 |
| `--bind-address` | 来源监听地址，默认 `127.0.0.1`，也可选 `::1` |
| `--target-address` | 相对于目标设备解析的服务地址，默认 `127.0.0.1` |
| `--auto-start` / `--no-auto-start` | 开启或关闭“管理器启动时自动建立此映射” |

`add` 需要两端设备和端口，建议同时用 `--name` 设置便于识别的名称；`edit` 只传要修改的字段。编辑映射不会重置它的置顶状态。

要调整顺序，先用 `list` 查看现有映射，再向 `reorder` 提供**全部映射**，例如只有 `demo`、`metrics` 两条映射时：

```sh
./jumper-manager mappings reorder demo metrics
```

输入时先列出全部置顶映射，再列普通映射，可调整各组内顺序；遗漏、重复或已失效的映射标识会被拒绝。排序和置顶会保存，不会重启隧道。

查看日志或获取 JSON 输出：

```sh
./jumper-manager logs --tail 50
./jumper-manager logs demo --tail 20
./jumper-manager list --json
./jumper-manager mappings show demo --json
```

`logs` 不带映射标识时读取服务日志，也可以写成 `logs --server`；指定映射标识时读取该映射的日志。服务日志在管理器停止后也可读取。`--json` 可放在命令最后，适合脚本处理；命令帮助可通过 `./jumper-manager --help` 或相应子命令的 `--help` 查看。

## 先建立映射，再启动目标服务

在 WebUI 选择来源设备、来源监听端口、目标设备和目标服务端口，保存后点击“启动”。例如：

```text
source-device 的 127.0.0.1:50052
    → 运行 JumperManager 的 Linux 设备中转
    → target-device 的 127.0.0.1:50051
```

成功建立的映射统一显示“已启动”。即使目标服务还没开，也可以先建立隧道；目标服务启动后即可通过已有映射访问，无需重建。

目标进程信息单独显示：

| 显示 | 含义 |
| --- | --- |
| 没有进程在用 | 检查时，目标地址和端口没有监听进程 |
| 有 N 个进程在用 | 检查到 N 个不同的监听进程，已按进程去重 |
| 进程数或进程状态未知 | 权限、容器命名空间或检查条件不足，无法给出完整数量 |

这些是启动或点击“检查”时的快照，不是客户端连接数量，也不表示业务请求一定成功。Linux 可能看得到监听端口，却看不到所属进程，此时显示未知。SSH 登录、入口端口或实际转发进程发生问题时，仍会显示启动失败或异常。

## 登录后自动启动

手动启动是默认方式。支持 `systemd --user` 的服务器可以选择为当前用户配置登录后自动启动：

```sh
./jumper-manager autostart status
./jumper-manager autostart enable
./jumper-manager autostart disable
```

`enable` 只注册并启用用户服务，**不会立即启动管理器**；需要运行时再执行 `./jumper-manager start`。`disable` 只取消以后自动启动，**不会停止当前管理器**；立即停止仍使用 `./jumper-manager stop`。

管理器处于停止状态时，已启用用户服务的 `start` 会通过 systemd 启动；未启用时使用普通后台进程。`autostart status` 会显示对应服务名。服务名包含安装目录的标识，因此不同目录的实例不会共用同一份用户服务配置。

如果使用自定义端口或 SSH 配置，在启用时一并指定，它们会保存到用户服务中：

```sh
./jumper-manager autostart enable --port 8876 --ssh-config "$HOME/.ssh/manager-config"
./jumper-manager start
```

这里的自定义配置文件必须已经存在，且包含需要使用的 SSH 别名。

这与单条映射的自动启动是两个设置：前者启动管理器，后者决定管理器启动后自动建立哪些映射。希望登录后自动恢复映射时，需要同时配置两者。

`systemd --user` 的默认行为是随用户登录启动。**机器开机后无人登录也要运行**，需要管理员另外为该用户配置 linger，例如经管理员确认后执行 `sudo loginctl enable-linger "$USER"`。JumperManager 不会默认开启 linger，也不会自行提权。启用用户服务不代表它会继承交互式终端的 `ssh-agent` 环境，应在实际自动启动环境中验证 SSH 登录。

## 保存配置、升级和排查

配置与日志保存在可执行文件同目录的 `data/`，例如 `~/JumperManager/data/`。升级前先停止管理器并备份 `data/`，再替换程序及随包文件，保留原来的 `data/`，最后重新启动。不要用其他设备的数据目录直接覆盖仍在运行的实例。

如果移动安装目录且曾开启登录后自动启动，先停止管理器，并在旧目录取消自动启动，再移动目录，在新位置重新启用，使用户服务引用正确的路径。

常见问题：

- **找不到 SSH 设备**：检查的是 Linux 当前用户的 `~/.ssh/config`，并确认其中有具体的 `Host` 别名。在 WebUI 同步设备列表后重试。
- **后台登录失败，但终端里能登录**：确认终端是否使用了密码交互、不同用户、不同 SSH 配置，或只有当前终端可用的 agent。
- **浏览器打不开**：先在服务器执行 `./jumper-manager status`，再确认访问 WebUI 的 SSH 转发仍在运行，且两端端口一致。
- **目标进程数量未知**：如果隧道已启动，可以继续尝试正常业务访问。目标数量受进程权限与命名空间可见性限制，不能根据监听端口直接猜测进程数。
- **修改 SSH 配置后路线没变**：已有隧道继续使用原连接；停止相应映射并重新启动，才会应用新配置。
- **独立程序提示缺少某个 GLIBC 版本**：当前二进制与系统库基线不兼容。请使用已验证的 Ubuntu 24.04 x86_64 环境；需要自行适配时，参阅 [源码说明](../SOURCE-LINUX.md)。

更多界面操作见 [README](../README.md)。
