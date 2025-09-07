# demo.md — 统计解释 & 去重判定 & 排错与调参

本文示例基于你本次运行获得的统计：

```json
{
  "pages_fetched": 403,
  "pages_skipped": 0,
  "pages_failed": 4,
  "chunks_written": 403,
  "chunks_deduped": 203,
  "queue_peek": 20126
}
```

---

## 1) 这些字段分别是什么意思？

- **pages_fetched = 403**：成功抓取并解析的页面数量（HTTP 2xx）。  
- **pages_failed = 4**：抓取失败的页面数（超时 / 4xx / 5xx）。  
- **pages_skipped = 0**：被 robots.txt 或过滤规则（include/exclude）跳过的页面数。  
- **chunks_written = 403**：生成的候选片段（chunk）数。若未开启分片（`--chunk-size 0`），通常“**每页=1 个 chunk**”。  
- **chunks_deduped = 203**：**被判定为重复**而**未写入 JSONL** 的 chunk 数。  
- **queue_peek = 20126**：抓取过程中，待抓取队列的**峰值长度**（发现的链接很多，不代表都会成功抓取；visited/过滤会去重/截断）。  

> 推导：实际写入 JSONL 的条目数 ≈ `chunks_written - chunks_deduped` ≈ `403 - 203 = 200`。  
> 因此看到 `page-201`（从 0 计数的第 202 条）是正常的；ID 是“页面序号”，并不等于 JSONL 的“行号”。

---

## 2) 什么是“判定重复”？

当前脚本使用的是**基于内容文本的精确去重**：  
- 对每个 chunk 的**清洗后文本**做 **SHA1 哈希**；  
- 如果某个哈希值**之前出现过**，该 chunk 视为“重复”，**不再写入 JSONL**，同时 `chunks_deduped += 1`；  
- 清洗动作包括移除 `script/style/nav/header/footer/aside` 等标签、压缩空白等。

**示意：**
```python
h = sha1(chunk_text.encode("utf-8")).hexdigest()
if h in chunk_hashes:   # 相同文本已写过
    deduped += 1
    continue            # 不写入
chunk_hashes.add(h)
```

**常见重复来源：**
- 模板/占位/导航页清洗后文本几乎一致；
- 同内容不同 URL（跳转/别名/镜像）导致正文完全一致；
- 目录/索引页“上一页/下一页”的固定文本。

---

## 3) 直观“重复”示例

**A 页面**：`/doc/usage/index.html`  
```
RoboTwin 2.0 Offical Document
# Usage
Welcome to RoboTwin usage guide...
```

**B 页面**：`/doc/usage/welcome.html`  
```
RoboTwin 2.0 Offical Document
# Usage
Welcome to RoboTwin usage guide...
```

两段文本完全一致 → 哈希一致 → **B 被判重**，不写入 JSONL。

---

## 4) 快速自查手册（命令行）

```bash
# 实际写入了多少行？
wc -l corpus_full.jsonl corpus_min.jsonl

# 查看哪些页面可能是模板/占位（清洗后字符数很短）
# 第6列 chars = 清洗后文本长度
awk -F, 'NR>1 && $6 < 200 {print $0}' manifest.csv | head

# 看失败页的 URL（如果需要）
grep -n "WARN\] \[\(4\|5\)[0-9][0-9\]" crawl.log | head

# 粗算去重率
python - << 'PY'
import json
s=json.load(open('stats.json','r',encoding='utf-8'))
total=s['chunks_written']
dup=s['chunks_deduped']
print('dedup_rate = {:.1%}'.format(dup/max(total,1)))
PY
```

---

## 5) 想把更多条目“保留下来”的几种方法

> 下面从“最简单”到“更精细”的顺序给出方案。你可以只选其一，也可以组合。

### 方案 A：**关闭去重**（保留所有 chunk）
适合“先把东西都抓下来，再后处理”。

**代码打补丁（添加开关）**：在 `robust_crawl_to_jsonl.py`：
```python
# argparse：
ap.add_argument("--no-dedupe", action="store_true", help="Do not drop duplicate chunks")

# Crawler.__init__：
self.no_dedupe = args.no_dedupe

# write_records() 中：
h = hash_text(ck)
if not self.no_dedupe and h in self.chunk_hashes:
    deduped += 1
    continue
self.chunk_hashes.add(h)
```

**运行：**
```bash
python robust_crawl_to_jsonl.py --no-dedupe
```

---

### 方案 B：**把“去重键”换成 `url + "\n" + contents`**（同文不同页也保留）
适合“正文相同但 URL 不同也希望各留一条”。
```python
# 原来：
h = hash_text(ck)
# 改为：
h = hash_text(url + "\n" + ck)
```
这样只会剔除“同一页内的完全重复片”，**不会**把“不同 URL 的相同正文”当成重复。

---

### 方案 C：**跳过超短页面**（减少模板/占位）
很多“被判重”的其实是模板/占位页。增加一个最小长度阈值：
```python
# argparse：
ap.add_argument("--min-chars", type=int, default=0, help="Skip writing if cleaned text shorter than this")

# Crawler.__init__：
self.min_chars = args.min_chars

# write_records() 开头：
if len(text or "") < self.min_chars:
    return 0, 0
```

**运行：**
```bash
python robust_crawl_to_jsonl.py --min-chars 200
```

---

### 方案 D：**开启分片（+ 少量重叠）**，降低“整页相同文本”的影响
同一页拆成多个块后，模板/导航只占其中一部分，其余“真正正文”更容易被保留。
```bash
python robust_crawl_to_jsonl.py --chunk-size 1000 --chunk-overlap 120
```
> 打开分片后，ID 会变成 `page-12-c0/c1/...`；后续检索建议配合“邻居扩展”。

---

## 6) 推荐起步参数

- `--chunk-size 1000 --chunk-overlap 120`（分片 + 15%~20% 重叠）  
- `--min-chars 200`（过滤占位页）  
- 如果要尽量**保留**同文不同 URL：使用 **方案 B**（`url+text` 哈希）。  
- 如果只是为了**先把东西全抓下来**：直接 **`--no-dedupe`**。

---

## 7) FAQ

**Q1：为什么 `queue_peek` 很大？**  
A：说明页面里发现了很多链接（菜单/目录/索引），不代表都抓了；visited/过滤/域内限制会剔除大部分。

**Q2：为什么 JSONL 的最后一行是 `page-201`？**  
A：`page-XXX` 是“页面计数”，并非“JSONL 行号”。被判重或过滤的页面 **不写入 JSONL**，但页面计数仍然递增。

**Q3：如何重新跑一遍？**  
- 保留历史并**增量**：加 `--resume`（会跳过已抓 URL，并在现有 JSONL 末尾追加）。  
- **重头再跑**：删除 `out_dir` 下的 `corpus_*.jsonl / manifest.csv / html/ / text/ / stats.json` 后重跑。

---

**结论**：你这次“403 → ~200 行”的现象，是由**内容去重**导致的正常结果。  
根据你的目标选择 A/B/C/D 中的一种调参方式，就能控制“保留多少条目”与“语料洁净度”的平衡。
