# 轨距读数交接 API

离线复核终端使用的交接服务：上传按采样顺序排列的轨距 CSV（整数微米），
填写**合格闭区间**、**逐点绝对误差上限**和**包的字节上限**，下载一个二进制包。
包自带解码信息（采样数、编码方式、合格区间、误差上限、CRC），重新上传
`/api/inspect` 即可按原顺序读出数值以及 **偏窄 / 合格 / 偏宽** 判定。

## 启动

```bash
docker compose up --build
# 打开 http://localhost:8000/docs 在页面上传文件（FastAPI Swagger UI）
```

登记包、补录原值都写入 SQLite；compose 把数据库放在命名卷
`handoff-data` 的 `/data/review.db` 上，容器重建后仍保留。不用 Docker 时：

```bash
python3 -m pip install -r requirements.txt
GAUGE_DB_PATH=data/review.db python3 -m uvicorn app.main:app \
    --host 0.0.0.0 --port 8000
```

`GAUGE_DB_PATH` 缺省为 `data/review.db`。`/healthz` 会真正读一次数据库：
可用时返回 `200` 且 `"database": "ready"`，不可用时返回 `503` 且
`"database": "unavailable"`，复核接口同时返回 `503`。

## 接口

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| POST | `/api/packages` | CSV + 三个限制 → 交接包（`application/octet-stream`） |
| POST | `/api/inspect` | 交接包（可选原始 CSV）→ 数值、判定、核对报告 |
| POST | `/api/review/packages` | 登记 `.tgp`（上传文件）→ 持久化包号 |
| GET | `/api/review/packages/{包号}` | 已登记包的信息与补录数量 |
| POST | `/api/review/packages/{包号}/rejudge` | 新合格闭区间下复判采样号范围 |
| POST | `/api/review/packages/{包号}/supplements` | 批量补录采样号与原值 |
| GET | `/healthz` | 健康检查（含 SQLite 可用性） |
| GET | `/docs` | 交互文档，可直接上传文件 |

`/api/packages` 的表单字段：

* `file`：CSV，每行一个整数，或单行多列；允许一个 `value`/`gauge` 表头；UTF-8。
* `lower` / `upper`：合格闭区间（微米，**含端点**）。
* `eps`：逐点绝对误差上限（微米）。`0` 表示必须无损。
* `byte_limit`：包的字节上限（含 48 字节头部）。

响应头 `X-Package-*` 给出采样数、实际字节数、使用的编码、
是否近似、实际最大误差，以及 `X-Package-Verified: true`
（只有在服务端已把包解码并与原值逐点核对通过后才会返回）。

失败时返回 JSON `{"detail": ...}`：`400` 表示 CSV/参数不合法，
`413` 表示在字节上限内无法交付完整包——此时**不会**返回任何部分数据。

## 复核流程（登记、复判、补录）

轨距合格区间调整后，复核员不需要重新解码整包，按下列顺序即可知道旧交接包
里哪些采样能直接复判、哪些必须找原始记录：

1. **登记**：`POST /api/review/packages` 上传 `.tgp`，返回从 1 起算的包号。
   包本身、逐点解码值与逐点**可能原值区间**都写入 SQLite。
2. **复判**：`POST /api/review/packages/{包号}/rejudge`

   ```json
   {"first_sample": 1, "last_sample": 500, "lower": 1434000, "upper": 1436000}
   ```

   返回该范围内每个采样号一条记录，`verdict` 为
   `narrow` / `pass` / `wide` / `unknown`（标签 `偏窄` / `合格` / `偏宽` / `待查`），
   并附 `counts` 汇总。采样号从 1 起、闭区间含两端。

   每条记录带：

   * `source`：`decoded`（按包内解码值）或 `supplement`（按补录原值）。
     补录点的 `value` 是原值，未补录的近似点 `value` 为 `null`；
   * `decoded`：包内解码值，便于和原值对照；
   * `possible`：该点可能原值的闭区间；
   * `certain`：结论是否确定；`unknown` 时 `candidates` 列出可能的判定。

3. **补录**：`POST /api/review/packages/{包号}/supplements`

   ```json
   {"samples": [{"sample_no": 17, "value": 1435003},
                {"sample_no": 23, "value": 1436001}]}
   ```

   已补录的点以后一律按原值判定（`source: "supplement"`），换区间后仍然有效。

### 复判规则

* **无损包**：以解码值为原值，结论总是确定（`narrow` / `pass` / `wide`）。
* **近似包**：某个采样的**可能原值**必须同时满足
  * 与解码值之差的绝对值不超过包内误差上限 `eps`；
  * 其**旧判定**（按包内记录的旧合格区间）与解码值的旧判定一致：
    旧 `pass` 落在旧闭区间内、旧 `narrow` 严格小于旧下界、
    旧 `wide` 严格大于旧上界。

  在这个可能区间上，如果所有值的**新判定一致**，直接返回该判定；否则返回
   `unknown`（待查），并在 `candidates` 里列出该区间覆盖的候选判定，复核员据此
  决定是找原始记录还是补录原值。绝不在可能原值给出不同结论时臆断一个判定。
* 无损包即使头部带有非零 `eps`，只要包未被标记为近似，仍以解码值为准。
* 判定区间始终是**闭区间**：`value == lower` 或 `value == upper` 都算合格。

### 补录规则（整批原子）

* 采样号无效（`< 1` 或超过包内采样数）、补录值超出该点可能区间、
  或同一采样号在一个批次里出现两个不同值（`异常值`）时，**整批退回**：
  响应 `400`，本批一行都不写，已存原值保持不变。
* 同一采样号重复提交**相同**值算成功（`stored` / `unchanged` 计数区分），
  补录只需查询涉及的采样，不会重新解码整包。
* 一次复判查询在单个事务内完成；在 WAL 模式下即使另一请求正在提交补录，
  整段查询取到的原值也来自同一个已提交快照，不会一半旧一半新。

## 判定与误差规则

* 判定按原值：`value < lower` → `narrow`（偏窄），`value > upper` → `wide`（偏宽），
  否则 `pass`（合格）。
* 包中每个还原值 `restored` 必须满足 `abs(restored - original) <= eps`。
* 包中每个还原值的判定必须与原值**完全一致**；近似算法会把每个样本约束在
  同时满足误差带和判定带的区间内，贴近合格边界的样本不会被移动过界。
* 先尝试无损编码；无损包装不下时才在误差限内近似；近似后仍装不下返回 `413`。

## 包格式（版本 1）

小端、无填充，头部固定 48 字节：

| 偏移 | 长度 | 字段 |
| --- | --- | --- |
| 0 | 4 | magic `TGPA` |
| 4 | 1 | 版本 = 1 |
| 5 | 1 | 编码 ID |
| 6 | 1 | 标志位（bit0 = 已近似） |
| 7 | 1 | 保留 = 0 |
| 8 | 4 | 采样数（uint32） |
| 12 | 4 | 负载长度（uint32） |
| 16 | 4 | 负载 CRC32 |
| 20 | 8 | 合格区间下界（int64，含） |
| 28 | 8 | 合格区间上界（int64，含） |
| 36 | 8 | 逐点绝对误差上限（int64） |
| 44 | 4 | 头部 CRC32（覆盖字节 0..43） |
| 48 | … | 负载 |

编码 ID：`1` raw-int32，`2` delta-zigzag-varint，`3` deflate(int32)，
`4` deflate(delta-varint)，`5` 二阶 delta zigzag varint。服务端对每种编码
都生成候选并选择能装进上限的最小者。

## 离线解码

复核终端可以不启动 Web 服务，仅用标准库解码：

```bash
python3 -I app/decode.py package.tgp                       # 序号 / 数值 / 判定
python3 -I app/decode.py package.tgp --csv                 # 仅数值，按原顺序
python3 -I app/decode.py package.tgp --original gauge.csv  # 与原值逐点核对
```

## 测试

```bash
python3 -m pip install pytest httpx   # 仅测试需要
python3 -m pytest -q
```

覆盖：闭区间端点判定、五种编码往返、CRC 损坏检测、CSV 边界输入、
字节上限两侧（恰好装入 / 少 1 字节则 413）、近似路径（误差与判定校验）、
无法装入时不交付半包、损坏包拒绝、API 端到端流程；复核部分覆盖登记取号、
新旧区间边界、近似包的确定/待查判定、补录整批退回与同值重传、
补录只查涉及采样、WAL 单快照读取，以及补录失败后已存原值保留。
