# sft — Smart File Transfer

[English](README.md) | **中文**

基于 SSH 的远程开发 CLI 工具。在远程主机上传输文件、挂载目录、执行命令和管理后台任务。

## 功能特性

- **智能文件传输**：根据文件数量和类型自动选择最优模式（rsync、tar+zstd、git bundle）
- **远程挂载**：基于 SSHFS 的目录挂载，支持生命周期管理
- **远程执行**：在远程主机上运行命令，自动同步环境
- **后台任务**：启动、监控和获取 nohup 后台任务的结果
- **PBS 集成**：提交和管理 HPC 集群任务
- **插件系统**：通过 `sft-nix`（Nix 环境同步）和 `sft-marimo`（notebook 编排）扩展功能

## 安装

### 从 Git 安装（推荐）

```bash
pip install git+https://github.com/StarsInDmajor/sft.git

# 连同插件一起安装
pip install git+https://github.com/StarsInDmajor/sft-nix.git
pip install git+https://github.com/StarsInDmajor/sft-marimo.git
```

### 通过 Nix 安装（NixOS 用户推荐）

sft 作为 flake input 使用。在 NixOS 配置中添加：

```nix
inputs = {
  sft.url = "github:StarsInDmajor/sft";
  sft-nix.url = "github:StarsInDmajor/sft-nix";
  sft-marimo.url = "github:StarsInDmajor/sft-marimo";
};
```

NixOS 配置会使用 `buildPythonApplication` + `makeWrapper` 将三个包构建为 `packages.sft`。

## 配置

创建 `~/.config/sft/config.yaml`：

```yaml
hosts:
  my-server:
    hostname: "192.168.1.100"
    port: 22
    user: "myuser"
    aliases: ["srv"]

  my-cluster:
    hostname: "10.0.0.50"
    port: 22
    user: "researcher"
    scheduler: "pbs"

transfer:
  compression_threshold: 50
  zstd_level: 2
  rsync_compression: "auto"
  rsync_options: ["-a", "--delete", "--partial"]

ssh:
  control_master: true
  control_path: "~/.ssh/sft-control-%h-%p-%r"
  control_persist: 600
  connect_timeout: 30
```

### 配置项说明

| 分组      | 键名                    | 类型     | 默认值                          | 说明                                       |
| --------- | ----------------------- | -------- | ------------------------------- | ------------------------------------------ |
| `hosts`   | （每个主机）             |          |                                 | 见下文                                     |
|           | `hostname`              | string   | 必填                            | IP 或域名                                  |
|           | `port`                  | int      | 22                              | SSH 端口                                   |
|           | `user`                  | string   | 必填                            | SSH 用户名                                 |
|           | `aliases`               | string[] | []                              | 主机别名，用于 `sft alias:~/path`          |
|           | `extra_options`         | map      | {}                              | 额外 SSH 选项（如 `IdentityFile`）          |
|           | `scheduler`             | string?  | null                            | 设为 `"pbs"` 启用 PBS 集群功能             |
| `transfer`| `compression_threshold` | int      | 50                              | 文件数超过此阈值时启用 zstd 压缩           |
|           | `zstd_level`            | int      | 2                               | zstd 压缩级别（1-22）                      |
|           | `rsync_compression`     | string   | `"auto"`                        | `auto` / `always` / `never`                |
|           | `rsync_options`         | string[] | `["-a","--delete","--partial"]` | 额外 rsync 参数                            |
| `ssh`     | `control_master`        | bool     | true                            | 启用 SSH 连接复用                          |
|           | `control_path`          | string   | `"~/.ssh/sft-control-%h-%p-%r"` | ControlMaster socket 路径                  |
|           | `control_persist`       | int      | 600                             | 空闲连接保持时长（秒）                      |
|           | `connect_timeout`       | int      | 30                              | SSH 连接超时（秒）                          |
| `logging` | `verbose`               | bool     | false                           | 默认详细输出                                |
|           | `log_file`              | string?  | null                            | 日志文件路径（null = 仅输出到 stderr）     |

## 使用方法

```bash
# 传输文件（自动选择最优模式）
sft ./project my-server:~/project

# 挂载远程目录
sft mount my-server:/path/to/dir

# 在远程主机上执行命令
sft run my-server "python train.py"

# 同步本地文件并在远程执行
sft sync-run ./project my-server:~/project -- python train.py

# 提交 PBS 任务
sft submit job.sh my-cluster

# 后台任务管理
sft run my-server --background "python train.py"
sft jobs
sft logs <job_id>
sft fetch <job_id>
```

## 插件架构

sft 使用 `importlib.metadata` 入口点（entry points）发现插件。插件在 `sft.plugins` 组下注册。

### 可用插件

| 插件          | 入口点                   | 功能                                                          |
| ------------- | ------------------------ | ------------------------------------------------------------- |
| **sft-nix**   | `sft_nix.hooks`          | `.envrc`/`flake.nix` 检测、nix develop 命令包装、传输后 flake 同步 |
| **sft-marimo**| `sft_marimo.hooks`       | `sft marimo start/stop/status/list` 子命令                     |

### 插件工作原理

1. 启动时，`sft.cli:main()` 调用 `plugins.discover_plugins()`
2. `discover_plugins()` 加载 `sft.plugins` 组中的所有入口点
3. 每个插件的入口点模块被导入后：
   - **sft-nix**：调用 `apply_overrides()` 替换 `sft.env` 中的桩函数
   - **sft-marimo**：调用 `register_subcommand("marimo", ...)` 添加 CLI 命令

### 注册 API

插件通过 `sft.plugins` 中的函数扩展 sft：

| 函数                             | 用途                             |
| -------------------------------- | -------------------------------- |
| `register_subcommand(name, fn)`  | 添加顶层 CLI 命令                |
| `register_subcommand_parser(name, fn)` | 注册子命令的 argparse 配置 |
| `register_env_resolver(fn)`      | 注册环境解析钩子                 |
| `register_post_transfer_hook(fn)`| 注册文件传输后的回调钩子         |

### 编写新插件

1. 创建 Python 包，在 `pyproject.toml` 中声明：

```toml
[project]
name = "sft-myplugin"
dependencies = ["sft"]

[project.entry-points."sft.plugins"]
myplugin = "sft_myplugin.hooks"
```

2. 创建 `src/sft_myplugin/hooks.py`：

```python
from sft.plugins import register_subcommand, register_subcommand_parser

def register():
    register_subcommand("mycommand", handle_mycommand)
    register_subcommand_parser("mycommand", add_my_parser)

def handle_mycommand(args, ctx):
    ...

def add_my_parser(subparsers, global_parent):
    parser = subparsers.add_parser("mycommand", parents=[global_parent])
    parser.add_argument("target")
```

3. 与 sft 一起安装——入口点会被自动发现。

## 开发

详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

### 快速开始

```bash
# 以可编辑模式安装（含开发依赖）
pip install -e ".[dev]"

# 运行 CLI
sft --list-hosts

# 运行测试
pytest tests/ -v
```

## 项目结构

```
sft/
├── src/sft/
│   ├── __init__.py
│   ├── __main__.py          # python -m sft 支持
│   ├── cli.py               # 参数解析、命令分发
│   ├── config.py            # 配置加载、主机定义、目标解析
│   ├── config_project.py    # 项目级 .sft/config 覆盖
│   ├── context.py           # ExecutionContext（SSH、dry-run、日志）
│   ├── env.py               # 环境检测桩函数（由 sft-nix 覆盖）
│   ├── manifest.py          # 传输清单（校验和、大小、mtime）
│   ├── background.py        # 后台任务状态管理
│   ├── pbs.py               # PBS 任务提交辅助
│   ├── plugins.py           # 插件注册表 + discover_plugins()
│   ├── shell.py             # Shell 引用工具
│   ├── state.py             # 状态目录管理
│   ├── transfer.py          # 文件传输逻辑（rsync、tar+zstd）
│   ├── ui.py                # 终端输出（主题、颜色、步骤）
│   └── commands/
│       ├── __init__.py
│       ├── mount.py          # sft mount / umount / mounts
│       ├── run.py            # sft run
│       ├── sync_run.py       # sft sync-run
│       ├── submit.py         # sft submit（PBS）
│       ├── jobs.py           # sft jobs / logs / stop / fetch / status
│       └── fetch.py          # sft fetch
├── tests/
│   ├── test_pure.py          # 单元测试（配置、解析、环境桩函数）
│   ├── test_transfer.py      # 传输逻辑测试
│   └── test_integration.py   # CLI 集成测试
├── config.yaml.example       # 示例配置
├── pyproject.toml
└── README.md
```

## 许可证

MIT
