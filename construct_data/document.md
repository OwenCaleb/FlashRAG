# Website Crawler → FlashRAG JSONL Corpus

该工具用于 **爬取指定网站文档**（如 [RoboTwin Docs](https://robotwin-platform.github.io/doc/)），并将页面转换为 **FlashRAG 可用的 JSONL 语料库**。  
支持 **断点续跑、日志记录、分块、去重、robots.txt、sitemap** 等特性，适合构建高质量知识库。

---

## ✨ 功能特性
- **实时日志**：控制台和文件中都有详细日志（带时间戳）。  
- **断点续跑**：`--resume` 自动跳过已抓取页面。  
- **robots.txt 支持**：`--respect-robots` 遵守网站爬虫规则。  
- **sitemap.xml 预热**：`--use-sitemap` 自动加载站点地图。  
- **分块 (chunking)**：按字符数拆分长文档，支持 overlap。  
- **去重**：对每个 chunk 计算哈希，避免重复内容写入。  
- **可审阅产物**：保存原始 HTML、清洗文本、JSONL、索引 CSV、统计 JSON、日志文件。  

---

## 📦 安装
```bash
# 建议使用虚拟环境
pip install -r requirements_robust.txt
```
依赖：`requests`、`beautifulsoup4`、`urllib3>=1.26.0`。

---

## 🚀 使用方法

### 默认运行
```bash
python robust_crawl_to_jsonl.py
```
等价于：
```bash
python robust_crawl_to_jsonl.py \
  --base-url https://robotwin-platform.github.io/doc/ \
  --out-dir ./corpus_out \
  --max-pages 2000000 \
  --delay 0.5 \
  --timeout 15.0 \
  --user-agent "FlashRAG-Crawler/1.0 (+1962672280@qq.com)"
```

### 推荐参数（更稳定/更安全）
```bash
python robust_crawl_to_jsonl.py   --respect-robots   --use-sitemap   --resume   --save-html --save-text   --chunk-size 1000 --chunk-overlap 120   --heartbeat-every 10   --log-level INFO
```

---

## 📂 输出目录结构
以 `--out-dir ./corpus_out` 为例：
```
corpus_out/
├── html/                 # 原始 HTML 页面
├── text/                 # 清洗后的纯文本
├── corpus_min.jsonl      # FlashRAG 最小格式 (id + contents)
├── corpus_full.jsonl     # 富信息版 (id, url, title, chunk_index, contents, hash)
├── manifest.csv          # 页面级索引 (id,url,title,html_path,txt_path,chars)
├── stats.json            # 运行统计 (抓取页数/失败数/chunk数等)
├── crawl.log             # 运行日志
└── README.txt            # 简要说明
```

---

## 📑 JSONL 格式示例
**最小版 corpus_min.jsonl**
```json
{"id": "page-0-c0", "contents": "安装依赖 -> 配置环境变量 -> 运行 docker-compose up"}
{"id": "page-1-c0", "contents": "常见问题：如何查看日志..."}
```

**完整版 corpus_full.jsonl**
```json
{
  "id": "page-0-c0",
  "url": "https://robotwin-platform.github.io/doc/usage/index.html",
  "title": "使用指南",
  "chunk_index": 0,
  "chunk_count": 2,
  "contents": "安装依赖 -> 配置环境变量 -> 运行 docker-compose up",
  "hash": "7bcd2f..."
}
```

---

## 📊 与 FlashRAG 对接
在 FlashRAG 的配置文件中，指向生成的 JSONL 即可：
```yaml
data:
  corpus:
    type: local_jsonl
    path: ./corpus_out/corpus_min.jsonl
```
如需向量检索，可以进一步用 `sentence-transformers` + `faiss` 构建索引。

---

## 📈 日志与监控
运行时会打印：
```
2025-09-07 15:12:01 [INFO] [1/2000000] Fetch #0: https://robotwin-platform.github.io/doc/ (queue=5)
2025-09-07 15:12:03 [INFO] [HEARTBEAT] pages=10 failed=0 chunks=25 deduped=2 queue~12 elapsed=20.5s
```
完整日志保存在：`corpus_out/crawl.log`。

---

## ⚠️ 注意事项
- 爬虫可选遵循站点 `robots.txt`（启用 `--respect-robots` 时）。  
- 建议合理设置 `--delay`，避免对目标网站造成压力。  
- 大规模抓取时推荐开启 `--resume`，避免因中断重复抓取。  
- 如需限定范围，可用 `--include-regex` 和 `--exclude-regex`。

---

## 🧩 常用参数速查
- `--base-url`：爬取起始地址（默认 RoboTwin 文档站）。
- `--out-dir`：输出目录，默认 `./corpus_out`。
- `--max-pages`：最大抓取页数，默认 `2000000`。
- `--delay`：抓取间隔秒数，默认 `0.5`。
- `--timeout`：HTTP 超时，默认 `15.0`。
- `--user-agent`：UA 标识，默认 `"FlashRAG-Crawler/1.0 (+1962672280@qq.com)"`。
- `--respect-robots`：遵守 robots 规则。
- `--use-sitemap`：从 sitemap.xml 预热种子链接。
- `--resume`：断点续跑（会追加写文件）。
- `--save-html` / `--save-text`：是否保存原始 HTML/清洗文本。
- `--chunk-size` / `--chunk-overlap`：分块大小与重叠。
- `--include-regex` / `--exclude-regex`：URL 过滤。
- `--heartbeat-every`：每抓 N 页打印一次心跳。
- `--log-level` / `--log-file`：日志级别与路径。

---

**祝使用顺利！** 如需扩展（并发抓取、PDF/OCR、认证访问、增量更新等），可以在该脚本基础上继续迭代。
