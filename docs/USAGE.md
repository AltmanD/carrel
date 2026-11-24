# 使用说明

安装和各系统启动命令见 [README](../README.md)。命令中的 carrel 指虚拟环境里的可执行文件；未激活环境时，Windows 使用 ./.venv/Scripts/carrel.exe，Linux/macOS 使用 ./.venv/bin/carrel。

## 导入与阅读

网页支持粘贴 arXiv/Hugging Face Papers 链接或混合文本，识别链接后确认标题和地址；也支持上传 PDF。选择处理深度：收录只建条目，速览生成简短理解，笔记分析主要章节，全文处理更完整内容。可在高级选项调整翻译、分类、评估和关系。

CLI 示例（与 Web 服务共用资料库时，先停止服务）：

    carrel --config config/local.toml --data-dir data import https://arxiv.org/abs/1706.03762 --depth skim
    carrel --config config/local.toml --data-dir data import /path/to/paper.pdf --depth full
    carrel --config config/local.toml --data-dir data batch /path/to/papers --workers 6

批次可使用终端输出的批次 ID 继续：

    carrel --config config/local.toml --data-dir data batch --resume batch-xxxxxxxxxxxx

本地目录同步保留集合和人工笔记，第一步只收录、不调用解析或模型：

    carrel --data-dir data sync /path/to/papers --dry-run
    carrel --data-dir data sync /path/to/papers

Windows 路径可用 D:/papers；含空格时加引号。论文页提供总结、原文与译文对照、关系三个页签，支持阅读批注、人工笔记、关键词和分类编辑。

## 图谱

关系页默认显示全库分组概况。按分类或阅读集合查看，分类／集合／关键词可组合筛选；组内默认展示 24 篇，不自动绘制全部连线。点击论文展开当前论文及最多 5 篇邻居，可增至 10/20。

动机脉络基于已保存的分析证据，每篇保留一个主要前驱，默认展开最多 12 篇。领域以文字和颜色表达，完整标题与补充关系在详情查看。方法名和主线名称支持人工修改，浏览不调用模型。SVG/PNG 导出只包含当前视口或已展开内容。

## 配置与维护

设置页可以添加模型提供方、选择 API 协议、分配任务并验证连接；处理任务页查看进度、失败原因与重试。API 连接测试及处理会使用服务商额度，普通浏览不调用模型。

    carrel --config config/local.toml --data-dir data health
    carrel --config config/local.toml --data-dir data usage
    carrel --data-dir data titles

health 会探测模型连接；titles 默认只读检查中文标题，只有显式 --repair 才备份并调用模型修复。

备份时先停止服务，再复制整个 data 目录，保留数据库、论文工件、集合与配置的一致性。仅删除可重建图谱缓存 .graph-projection-v2.json 不会丢失论文；下次启动会重建。不要删除 source.pdf、app.db、笔记或分类文件来解决页面缓存问题。

将旧版分类迁移为两级分类时，先生成差异预览，再按已审阅清单执行：

    python scripts/migrate_taxonomy_v2.py dry-run --data-dir data --manifest migration.json
    python scripts/migrate_taxonomy_v2.py apply --data-dir data --manifest migration.json

执行前必须停止服务。工具提供 resume/rollback、输入冲突检查和备份；未知旧术语不会自动猜测为关键词。迁移清单含工件原值，应与资料库一起妥善保存，不提交到 Git。
