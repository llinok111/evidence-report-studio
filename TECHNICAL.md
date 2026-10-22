# evidence-report-studio · 技术说明

> 一句话：把「AI 写报告」从**看起来专业**，变成**不可能被编出来**。
>
> 上游 `deep-research`（MIT / 605★）解决的是「联网搜资料写报告」，它自己 README 里把痛点写得很清楚：
> *AI fabricates numbers → looks reasonable but has no traceable source*。
> 本项目补的就是这一环：**每条论断必须逐字回溯到原始语料记录，回溯不了不许进报告。**

---

## 1. 为什么值得改这一层

翻上游 `tools/dr_check.py`（480 行，10 个检查维度：编码、字数、JSON、标题层级、章节编号、元数据、目录、尾部、年份密度、数据池、深度均衡），会发现：

**没有一个维度做「论断 ↔ 来源原文」的实质比对。** 它的溯源深度只有这一条：

```python
url_val = fact.get('url')
if not url_val or not str(url_val).strip():
    issues.append(f"Record {i} fact {j}: 'url' is empty or null")
```

只要求 URL 非空。

这不是偷懒，是**结构性限制**：它的数据源是网页，网页会变、会 404、会被改写，逐字比对做了也留不住，下次校验必然失败。所以只能退到"有个链接"。

而本地语料是**不可变的 CSV**。原文永远不变 → 逐字比对 100% 可做 → 这条护城河只有本地语料场景能挖。

---

## 2. 改动边界：上游一行没动

这是本项目最重要的工程约束。**新增，不修改。**

```
$ sha256sum 对比 11 个上游文件
  一致   tools/dr_check.py      一致   SKILL.md
  一致   tools/dr_gen.py        一致   RULES.md
  一致   tools/dr_tools.py      一致   TYPES.md
  一致   tools/lang_config.py   一致   profiles.json
  一致   tools/generate_pages.py 一致  sources.json
  一致   LICENSE
```

好处是兼容性不需要论证——上游怎么跑还怎么跑，我们只是多挂了两条新路径。以后上游更新，直接覆盖同名文件即可，不产生冲突。

唯一被动过的上游文件是 `README.md`——它被改名成 `UPSTREAM-README.md`，**内容一字未改**。
理由很简单：仓库首页得介绍这个仓库，不能挂着一份介绍别的项目的中文文档。这是"改名"不是"改内容"——git blob 哈希与上游完全相同（`152a25f6`），只是路径变了。

新增文件：

| 文件 | 作用 |
|---|---|
| `tools/dr_records.py` | records 数据源：结构化语料表 → 上游数据池格式 |
| `tools/dr_trace.py` | 引用溯源层：`check` / `convert` / `audit` / `quote` |
| `README.md` | 本仓库首页 |
| `TECHNICAL.md` | 本文 |
| `NOTICE` | 上游归属与改动说明 |
| `docs/*.svg` | 数据流图、溯源深度对比图 |
| `example/` | 一套完整中间产物，可对照复现 |
| `reports/zh/` | 成品报告 |

---

## 3. 数据流

```
records.csv（建库.py 产出，1552 条）
   │
   ├─ dr_records.py sample ──→ sample.md       给大纲 agent 读的抽样
   │
   ├─ outline.json（大纲：主题 + 关键词）  ←── 由 sample 产出
   │
   ├─ dr_records.py build ──→ data-pool.json
   │      按大纲主题给记录分簇，每条 fact 带 rec_id + quote
   │      过上游原生 check-datapool → PASS
   │
   ├─ prepare_chapter / 章节撰写 ──→ chapters/chapter-N.md
   │      论断行尾写 [record_id]，不写序号
   │
   ├─ assemble-report ──→ report.md            上游装配，PASS
   │
   ├─ dr_trace.py check ──→ 逐字回溯，任一不过即 FAIL
   ├─ dr_trace.py audit ──→ 口径审计
   ├─ dr_trace.py convert ─→ [(N)](#refN) + 参考来源段
   │
   └─ generate_pages.py ──→ reports-browser/ + gh-pages/
```

关键设计：**主题由大纲驱动，不自己瞎聚类。** 语料的 `topic` 字段是"每句一个标签"（1526 个 topic / 1552 条记录），无法直接聚合。所以先出大纲，再按大纲给记录分簇。

---

## 4. 三个校验，逐个说

### 4.1 `check` — 逐字回溯

对正文里每个 `[record_id]`：

| # | 校验 | 不通过的含义 |
|---|---|---|
| 1 | record_id 在语料表里存在 | 这个 ID 是编的 |
| 2 | record_id 在本次数据池里 | 越界引用（没检索到却引用了） |
| 3 | 该条 fact 的 quote 能在原文里**逐字定位** | 引文对不上原文，标签是编的 |
| 4 | 数字落地：正文的量化数字必须出现在被引用记录的原文里 | **论断可以含糊，数字编不出来** |
| 5 | 引用密度 ≥30% 段落 | 大段在自由发挥 |

第 3 条定位时会先精确找，找不到再去掉所有空白重找（模型常少写/多写空格），仍找不到才判 FAIL。

第 4 条是最硬的一条，也是上游完全没有的。实测它抓到了我自己的两处错误：把 `2.5` 的论断引到了不含 `2.5` 的记录上，把 `8台` 引到了只讲 B300 的记录上。

### 4.2 `audit` — 口径审计

利用语料的 `claim_type`（事实陈述 / 个人判断 / 预测 / 传闻）：

**含数字的论断，其引用来源必须是「事实陈述」。**

一条"去年投放 8500 万"如果是说话人自己的说法，引用它就是在把观点当数据用。这是上游没有的概念——它只有笼统的 `confidence`。

但允许**明确标注的转述**：句子里有「他说」「据介绍」「原话」「他预计」这类标记，就不算违规。**允许转述，禁止把转述当客观事实** —— 这正是「不虚构」的分界线。

### 4.3 `convert` — 引用落地

把 `[record_id]` 按正文首次出现顺序编号成 `[(N)](#refN)`，生成参考来源段：

```
(1) `7699100533977582374-0227` · 2026-10-21 23:30:01 · 嘉宾
    — 原文「他们卖2000块钱 一个月卖给经销商 一年平均要2万多 厂家给一半钱 经销商要出」
```

**为什么用 record_id 而不是上游的「首次出现顺序编号」**：顺序编号在事实顺序变化时会整体错位，而且没有任何机制能发现错位。稳定 ID 不会错位——`7699100533977582374-0227` 永远是那一条。

顺带修了上游一处不精确：元数据里的「共引用 N 个来源」用的是数据池条目数（131），不是正文实际引用数（48）。一份正文引 48 条、元数据写 131 个来源的报告，恰好是本项目要消灭的那种不精确。

---

## 5. 实测数据（2026-10-21「企智AI落地派」直播）

| 环节 | 数值 |
|---|---|
| 输入语料 | `records.csv` 1552 条 / 4343 行转写 |
| 有效记录 | 1206（丢弃 346 条闲聊/附和/过短） |
| 大纲主题 | 8 个（4 章 × 2 节） |
| 主题匹配 | 平均得分 0.45–0.84；未匹配 1058 条（多为噪音与离题） |
| 入池事实 | 131 条（每条带 rec_id + quote） |
| 报告成品 | 269 行 / 6763 字 / 4 章 |
| 引用 | 51 处（去重 48），**逐字通过 48/48** |
| 段落覆盖 | 30/42 = 71% |
| 口径分布 | 事实陈述 76.5% / 个人判断 19.6% / 预测 3.9% |
| 校验结论 | `check` PASS、`audit` PASS、上游 `assemble-report` PASS、上游 `check-datapool` PASS |

### 校验器抓到的真实问题（不是构造的）

| 类型 | 实例 | 谁抓到的 |
|---|---|---|
| 引用错位 | 把 `2.5 能生成 30 秒视频` 引到了不含 2.5 的记录 `0168` | `check` 数字落地 |
| 引用错位 | 把 `8 台方案` 引到了只讲 B300 的记录 `0064` | `check` 数字落地 |
| 推断混入事实 | 我自己在正文里写「疑似 1.6 亿」，原文只有「1 6 亿」 | `check` 数字落地 |
| 口径越界 | 把「至少能卖 100 个客户」当结论写，来源是预测 | `audit` |

### 误报与修正

首轮 4 个 FAIL 里有 1 个是误报：`H3`、`B300`、`720P` 这类型号里的数字被当成"量"。已加判定：数字前后紧邻 ASCII 字母 → 标识符，跳过；纯 `x.y` 无单位 → 版本号，跳过。

---

## 6. 已知限制（如实列）

1. **主题匹配靠字符 bigram，不是语义**。大纲关键词与语料用词差异大时匹配不上（实测 1058 条未匹配，其中多数确实是噪音，但会有漏）。
2. **中文数字量词不被审计**。「五折」「一万块」「三个月」不含阿拉伯数字，`audit` 检测不到。这是当前实现的边界。
3. **`audit` 的转述判定是关键词表**，不是语义判断。「他说」这类标记靠白名单，句子结构复杂时会漏判或误判。
4. **引用密度阈值 30% 是经验值**，不同文体要调。
5. **上游的 4 阶段流水线需要 agent 编排**。本项目把数据源、校验、落地这三层做成了可独立运行的 CLI，但「大纲 agent / 章节 agent」那一层仍是上游 SKILL.md 的编排逻辑，本项目未接管。
6. **`prepare_chapter` 的中文匹配上游本身就有问题**（`sq.split()` 对中文返回整句，子串判断几乎永远为假）。本项目绕开了它——`dr_records.py` 自己做匹配。上游那处未修，因为要保持零改动。

---

## 7. 复现

```bash
# 0) 语料（来自 douyin-living）
python ../douyin-living/建库.py data/<room>

# 1) 抽样 → 人工/LLM 出 outline.json
python tools/dr_records.py stats  --records records.csv
python tools/dr_records.py sample --records records.csv --n 160 --out _work/sample.md

# 2) 按大纲建池，并过上游原生校验
python tools/dr_records.py build --records records.csv \
    --outline _work/outline.json --out _work/data-pool.json
python tools/dr_tools.py check-datapool _work/data-pool.json --mode standard

# 3) 写章节（每章 _work/chapters/chapter-N.md，论断行尾写 [record_id]）

# 4) 装配
python tools/dr_tools.py assemble-report --outline _work/outline.json \
    --chapters-dir _work/chapters --datapool _work/data-pool.json \
    --mode standard --target-year 2026 --output _work/report.md

# 5) 校验 → 审计 → 落地
python tools/dr_trace.py check   --report _work/report.md --pool _work/data-pool.json --records records.csv
python tools/dr_trace.py audit   --report _work/report.md --pool _work/data-pool.json --records records.csv
python tools/dr_trace.py convert --report _work/report.md --pool _work/data-pool.json --records records.csv

# 6) 报告浏览器
python tools/generate_pages.py     # → reports-browser/index.html
```

---

## 8. 许可

上游 `deep-research` 为 MIT License，Copyright (c) 2026 hoolulu，`LICENSE` 原文未改动。
新增部分版权声明见 `NOTICE`。
