# 开发与测试

Carrel 使用 Python 3.11+、FastAPI/Jinja2 和本地 JavaScript/CSS。运行无需 Node 构建步骤；浏览器依赖及其许可声明随 Python 包提供。

在仓库根目录创建并激活虚拟环境，安装开发依赖：

    python -m pip install -e ".[dev]"
    python -m pytest -q -m "not browser"

浏览器回归需要 Playwright 和 Chromium：

    python -m pip install -e ".[dev,browser]"
    python -m playwright install chromium
    python -m pytest -q -m browser

Linux CI 使用 python -m playwright install --with-deps chromium 安装系统依赖。GitHub Actions 覆盖 Windows、Linux、macOS 的 Python 3.11/3.14，以及独立的 Chromium 渲染检查。测试使用临时库和模拟服务；无需 API Token。

安装后运行 python scripts/check_install.py，检查 CLI 能否创建临时空库并提供页面、字体和 Worker。它也会在每个平台的 CI 中执行；独立的构建任务检查 wheel 安装后的相同行为。

## 代码结构

| 目录 | 职责 |
| --- | --- |
| src/carrel/web | Web 页面、API 与浏览器资源 |
| src/carrel/pipeline.py、scheduler.py | 分阶段处理、并行批次与恢复 |
| src/carrel/model | 模型协议、路由、限速与验证 |
| src/carrel/parse、structure、translate | PDF 解析、内容抽取、总结与翻译 |
| src/carrel/classify、graph、motivation | 分类关键词、关系图与动机脉络 |
| src/carrel/store、sync、web/local_sync.py | 数据库、原子写入、锁、本地目录同步 |
| tests | 离线单元与集成测试 |
| scripts | 合成库检查、迁移与提供方验证工具 |

运行资料库、密钥、备份、模型原始响应和探针产物不提交。使用 config/example.toml 创建自己的配置，在独立 data 目录测试。对已有资料库执行写入迁移前先停止服务，并保存备份。

构建发行包：

    python -m pip install build
    python -m build

构建后确认 wheel 包含模板、taxonomy.toml、布局 Worker、PDF.js、KaTeX 字体及第三方许可文件。Carrel 自有代码采用根目录 MIT 许可证；第三方资源按各自的许可发布，清单见 [vendor README](../src/carrel/web/static/vendor/README.md)。
