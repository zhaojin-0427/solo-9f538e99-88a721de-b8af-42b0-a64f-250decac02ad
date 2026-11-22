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

不需要 Docker 时：

```bash
python3 -m pip install -r requirements.txt
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 接口

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| POST | `/api/packages` | CSV + 三个限制 → 交接包（`application/octet-stream`） |
| POST | `/api/inspect` | 交接包（可选原始 CSV）→ 数值、判定、核对报告 |
| GET | `/healthz` | 健康检查 |
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
无法装入时不交付半包、损坏包拒绝、API 端到端流程。
