# Carrel

## Do What

个人论文阅读库。导入 arXiv、Hugging Face Papers、PDF 链接或本地 PDF，生成总结、中文翻译、两级分类与关键词，在本地 Web 中阅读、批注、管理阅读集合，并查看论文关系与研究动机脉络。支持目录同步、并行导入及收录／速览／笔记／全文四种处理深度。

## Quick Start

需要 **Python 3.11+** 和 Git。先克隆仓库，再按操作系统安装：

```sh
git clone https://github.com/AltmanD/carrel.git
cd carrel
```

### Windows（PowerShell）

```powershell
py -3 -m venv .venv
./.venv/Scripts/python.exe -m pip install -e .
if (!(Test-Path config/local.toml)) { Copy-Item config/example.toml config/local.toml }
```

编辑 `config/local.toml`，填写下方列出的 Token 和模型配置，然后启动：

```powershell
./.venv/Scripts/carrel.exe --config config/local.toml --data-dir data serve --port 18765 --open
```

如果没有 `py` 命令，可将第一行的 `py -3` 换成 `python`，并确认版本至少为 3.11。以上命令无需激活虚拟环境，也无需修改 PowerShell 执行策略。

### Linux

```sh
python3 -m venv .venv
./.venv/bin/python -m pip install -e .
test -f config/local.toml || cp config/example.toml config/local.toml
```

编辑 `config/local.toml` 后启动：

```sh
./.venv/bin/carrel --config config/local.toml --data-dir data serve --port 18765 --open
```

Debian／Ubuntu 若提示缺少 venv，先安装与 Python 版本对应的 `python3-venv` 包。没有桌面环境时省略 `--open`；在远程机器运行时，可用 `ssh -L 18765:127.0.0.1:18765 user@host` 转发后从本机浏览器访问。

### macOS（Terminal，zsh／bash）

先通过 [python.org](https://www.python.org/downloads/macos/) 或 Homebrew 安装 Python 3.11+，然后执行：

```sh
python3 -m venv .venv
./.venv/bin/python -m pip install -e .
test -f config/local.toml || cp config/example.toml config/local.toml
```

编辑 `config/local.toml` 后启动：

```sh
./.venv/bin/carrel --config config/local.toml --data-dir data serve --port 18765 --open
```

三种系统均打开 **http://127.0.0.1:18765**，点击「导入论文」开始使用。在运行终端按 `Ctrl+C` 停止；以后在仓库目录执行对应的启动命令即可，不必重新安装。端口被占用时可改为 `--port 18766`。论文、笔记和设置保存到 `data/`；使用已有资料库时，将 `--data-dir data` 换成其实际路径。

模型配置示例见 [config/example.toml](config/example.toml)。支持 `messages`、`chat-completions`、`responses` 协议；`base_url`、`api_key` 和 `name` 应与所用服务商匹配。启动后也可在「设置」中添加提供方、分配任务和测试连接。仅启动界面或浏览已导入的论文无需 Token。

## API Tokens

| 服务 | 何时需要 | 配置项 | 获取方式 |
| --- | --- | --- | --- |
| MinerU | PDF 解析与内容处理 | `[mineru] token` | 登录 [MinerU API 管理](https://mineru.net/apiManage)，创建 Token。PDF 解析使用云端 API。 |
| 模型服务 | 总结、翻译、分类与动机分析 | `[model] api_key` | 在所用服务商或兼容网关的控制台创建 API Key，例如 [OpenAI API Keys](https://platform.openai.com/api-keys) 或 [Claude Console](https://platform.claude.com/settings/keys)，并填写对应 API 地址与模型 ID。 |
| OpenAlex | 引用与被引关系，建议配置 | `[openalex] api_key` | 注册并验证邮箱后，在 [Settings → API](https://openalex.org/settings/api) 获取 Key。 |
| Sciverse | 相似论文检索，可选 | `[sciverse] token` | 登录 [Sciverse Console → Tokens](https://sciverse.space/tokens)，创建 API Token。 |

`config/local.toml`、资料库和提供方密钥文件均已加入 `.gitignore`。首次发布包含源码与测试，不附带个人论文数据或 API Token。项目采用 [MIT 许可证](LICENSE)；更多操作见 [使用说明](docs/USAGE.md)，测试与构建见 [开发说明](docs/DEVELOPMENT.md)。
