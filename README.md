# Flight Fare Watch

基于 Google Flights（SerpApi）或 Amadeus GDS 的低价机票查询与巡检工具。它支持固定航线、日期窗口、任意目的地发现、开口程、多段联程、拆票自转和可选低价枢纽接驳；每次执行生成 PDF 人读报告与 JSON 审计文件。

> 票价与库存均为检索时的指示信息。预订前必须在航司或正规 OTA 确认总价、航段、行李、签证、退改规则和出票时限。

## 功能概览

- 低价查询：单程、固定往返、日期窗口内约 N 天往返。
- 目的地发现：从指定机场搜索任意目的地的相对折扣机票。
- 复杂行程：开口程、2–6 段同票联程、OPEN 回程代理、拆票自转。
- 枢纽比价：从实际出发地直出；可选比较 ICN 等低价枢纽，并核算接驳票。
- 价格监控：SQLite 历史、降价/新低/目标价/市场低位提醒。
- 输出：终端 Markdown、text 或 JSON；本地 PDF + JSON；可选 SMTP 邮件附件。
- 调度：macOS launchd 或 Linux crontab，按 watch 独立安装。

## 目录结构

```text
flight-fare-scanner/
├── SKILL.md                    # AI 调用规范
├── README.md                    # 本文档
├── reference.md                 # Provider 契约与深入排障
├── config.example.yaml          # 完整配置示例
├── credentials.env.example      # 凭证模板
├── fixtures/                    # 离线测试数据
└── scripts/
    ├── fare_watch.py            # CLI 主入口
    ├── strategies.py            # 查询策略与校验
    ├── store.py                 # SQLite 历史与告警
    ├── notify.py                # PDF / JSON / SMTP 输出
    ├── city_airports.py         # 城市到机场的本地映射
    ├── airport_transit.py       # 中转机场中文名与规划预留建议
    ├── visa_cache.py            # 官方签证/过境核验缓存读取器
    ├── install_schedule.sh      # 定时任务安装器
    └── providers/               # Google Flights、Amadeus、mock provider
```

运行状态默认保存在：

```text
~/.flight-fare-scanner/
├── config.yaml
├── credentials.env
├── fares.sqlite3
├── reports/
└── watch-<watch-id>.log
```

## 在 Codex 中安装

### 前置条件

- Python 3.9 或更高版本。
- Google Flights 路径需要 SerpApi Key；Amadeus 路径需要 Amadeus API 凭证。
- PDF 报告需要 `reportlab`：

```bash
python3 -m pip install reportlab
```

### 安装技能与初始化

Codex 会从 `$CODEX_HOME/skills` 发现技能，默认目录为
`~/.codex/skills`。将本仓库中包含 `SKILL.md` 的目录安装为：

```text
$CODEX_HOME/skills/flight-fare-scanner/
```

如果使用 Codex 内置的技能安装器，可从 GitHub 仓库安装包含 `SKILL.md`
的路径；安装后重新打开或刷新 Codex。

macOS/Linux：

```bash
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
SKILL="$CODEX_HOME/skills/flight-fare-scanner"
STATE="$HOME/.flight-fare-scanner"

mkdir -p "$STATE"
cp "$SKILL/config.example.yaml" "$STATE/config.yaml"
cp "$SKILL/credentials.env.example" "$STATE/credentials.env"
chmod 600 "$STATE/credentials.env" "$STATE/config.yaml"
```

Windows PowerShell：

```powershell
$CodexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
$Skill = Join-Path $CodexHome 'skills\flight-fare-scanner'
$State = Join-Path $HOME '.flight-fare-scanner'
New-Item -ItemType Directory -Force $State | Out-Null
Copy-Item (Join-Path $Skill 'config.example.yaml') (Join-Path $State 'config.yaml')
Copy-Item (Join-Path $Skill 'credentials.env.example') (Join-Path $State 'credentials.env')
```

### 配置凭证

编辑 `~/.flight-fare-scanner/credentials.env`：

```bash
# 默认 provider：Google Flights via SerpApi
export SERPAPI_KEY="你的 SerpApi Key"

# 仅当 provider.name: amadeus 时需要
# export AMADEUS_CLIENT_ID="..."
# export AMADEUS_CLIENT_SECRET="..."

# 仅当启用 SMTP 邮件时需要
# export SMTP_USER="..."
# export SMTP_PASSWORD="应用专用密码"
```

不要把 Key、SMTP 密码或 cookie 写进 `config.yaml`、README、Git 仓库或对话。

加载凭证（macOS/Linux）：

```bash
source ~/.flight-fare-scanner/credentials.env
```

PowerShell 用户请将 `credentials.env.example` 中需要的变量设置到当前
会话，例如：

```powershell
$env:SERPAPI_KEY = '你的 SerpApi Key'
```

不要在 PowerShell 中直接执行含有 `export` 的凭证文件。

## 开始前的需求确认

真实查询会消耗额度。创建或执行 watch 前，请明确：

1. 查询类型：单程、固定往返、日期窗口往返、任意目的地发现，还是开口程/多段联程。
2. 实际出发机场、目标机场、附近机场是否接受。
3. 固定日期，或日期窗口与停留天数。
4. 舱位、乘客数、最大中转、航司范围与目标价格。
5. 若比较低价出发枢纽：是否接受独立 PNR、哪些枢纽可选、最小接驳缓冲。
6. 若安装巡检：频率与月度 API 调用预算。

自然语言城市应先核对常用商业机场。定时执行不会实时联网搜索城市代码；已内置少量映射，例如 `LON → LHR,LGW`。不在映射中的城市请配置明确机场代码。

## Provider 选择

| 能力 | `google_flights` | `amadeus` |
|---|---|---|
| 数据源 | Google Flights via SerpApi | Amadeus GDS API |
| 凭证 | `SERPAPI_KEY` | Client ID / Secret |
| 多机场 | 任意数量 | 主机场加最多 2 个备选 |
| 联盟筛选 | 支持 | 不支持 |
| 市场价格水位 | `low` / `typical` / `high` | 不支持 |
| 强制/排除中转点 | 不支持 | 支持 |
| 日期窗口最优组合 | 本地锚点扫描 | `cheapest_dates` 一次调用 |
| 可退改/无罚金筛选 | 不支持 | 支持 |
| 座位数 | 不支持 | 支持 |

默认配置：

```yaml
provider:
  name: google_flights
  market: hk
  language: en
  deep_search: false
  resolve_return: false
```

`resolve_return: true` 会额外请求一次以尝试展开回程明细，调用成本增加。

Skyscanner 未接入：其 Flights API 是合作伙伴准入模式，无法作为个人机器的自助 provider。

## 首次验证与基本命令

```bash
S=$CODEX_HOME/skills/flight-fare-scanner/scripts
CFG=~/.flight-fare-scanner/config.yaml

# 零调用：校验配置、策略、日期、provider 能力与调用次数
python3 "$S/fare_watch.py" validate --config "$CFG"

# 零调用：显示实际请求体；确认机场展开、日期和筛选条件
python3 "$S/fare_watch.py" validate --config "$CFG" --show-body

# 使用 fixture 跑完整离线流程，不消耗 API 额度
python3 "$S/fare_watch.py" run --config "$CFG" \
  --mock $CODEX_HOME/skills/flight-fare-scanner/fixtures/google-flights-hkg-lon.json

# 查询指定 watch
python3 "$S/fare_watch.py" run --config "$CFG" --watch hk-prd-lon-scout

# 输出格式：markdown（默认）/ text / json
python3 "$S/fare_watch.py" run --config "$CFG" --format text
python3 "$S/fare_watch.py" run --config "$CFG" --format json

# 只本地输出和报告，不调用邮件或钉钉
python3 "$S/fare_watch.py" run --config "$CFG" --no-notify

# 查询历史
python3 "$S/fare_watch.py" history --config "$CFG" --watch hk-prd-lon-scout

# 查看 provider 能力表
python3 "$S/fare_watch.py" providers --config "$CFG"
```

stdout 只输出报告；进度、PDF 与 JSON 路径在 stderr。若需要干净的报告：

```bash
python3 "$S/fare_watch.py" run --config "$CFG" --format text 2>/dev/null
```

## 基础配置字段

```yaml
defaults:
  currency: CNY
  adults: 1
  children: 0
  infants: 0
  travel_class: ECONOMY       # ECONOMY / PREMIUM_ECONOMY / BUSINESS / FIRST
  max_stops: 2                # 只允许 0 / 1 / 2
  max_offers: 40
  max_price: null
  airlines: []                # 2 位 IATA 航司白名单
  exclude_airlines: []
  alliances: []               # STAR_ALLIANCE / SKYTEAM / ONEWORLD
  layover_minutes: null       # [min, max]，Google Flights 路径
```

`airlines`、`exclude_airlines` 与 `alliances` 不可任意叠加。全服务航司建议使用显式白名单，例如：

```yaml
airlines: [CX, BA, VS, LH, LX, AF, KL, AY, QR, EK, EY, TK, SQ, CA, MU, CZ]
```

Google Flights provider 会在收到结果后再次本地校验白名单，避免未识别或不允许的营销航司混入结果。

## 查询策略

### 固定单程

```yaml
- id: hkg-nrt-oneway
  label: 香港至东京单程
  strategy: oneway
  origin: HKG
  destination: NRT
  depart: 2027-04-05
  max_stops: 0
```

### 固定往返与附近机场

```yaml
- id: hkg-london-roundtrip
  label: 香港至伦敦往返
  strategy: roundtrip
  origin: HKG
  destination: LHR
  nearby_destinations: [LGW]
  depart: 2027-04-01
  return: 2027-04-08
  target_price: 5000
```

`nearby_origins` 与 `nearby_destinations` 可用于每个普通航段或 `multi_city.legs[]`。例如上例实际请求 `arrival_id=LHR,LGW`。

### 日期窗口内的低价往返

适用于“某两个月内出发、停留约 7 天”的场景：

```yaml
- id: hkg-lon-flex
  label: 香港至伦敦七天往返
  strategy: flex_roundtrip
  origin: HKG
  destination: LHR
  nearby_destinations: [LGW]
  depart_window:
    from: 2027-04-01
    to: 2027-05-31
    step_days: 7
    max_probes: 12
  trip_days: 7
```

调用数为“日期锚点数 × 停留天数选项数”。锚点之间的日期不会被报价；减小 `step_days` 会提高精度和调用量。

### Amadeus 一次日期扫描

仅 Amadeus：

```yaml
- id: amadeus-date-scout
  strategy: cheapest_dates
  origin: HKG
  destination: LHR
  depart_window: { from: 2027-04-01, to: 2027-05-31 }
  trip_days: 7
```

它返回日期和价格，通常不返回可直接比较的完整航班明细；适合先找日期，再用 `roundtrip` 核验。

### 任意目的地相对低价发现

```yaml
- id: hkg-relative-deals
  strategy: low_fare_discovery
  origin: HKG
  home_country: China
  discovery_type: roundtrip
  outbound_window: { from: 2027-04-01, to: 2027-05-31 }
  travel_duration: 1
  max_deals: 20
```

该策略使用 Google Flights Deals，**不指定目的地**，结果按折扣比例降序、价格升序排序。`home_country` 仅标记国内/国际，不做价格过滤。单程请使用 `discovery_type: oneway` 并移除 `trip_length` 与 `travel_duration`。

### 开口程

```yaml
- id: europe-open-jaw
  strategy: open_jaw
  outbound: { from: HKG, to: CDG, date: 2027-04-01 }
  inbound:  { from: FCO, to: HKG, date: 2027-04-15 }
```

地面交通段不包含在机票价格中。

### 多段同票联程

```yaml
- id: south-america-multicity
  strategy: multi_city
  legs:
    - { from: HKG, to: LIM, date: 2027-04-01 }
    - { from: LIM, to: SCL, date: 2027-04-10 }
    - { from: SCL, to: GIG, date: 2027-04-20 }
    - { from: GIG, to: HKG, date: 2027-04-28 }
```

同一张票最多 6 段；每段最多 2 次中转。超过该范围需拆成多张票或分多个 watch。

### OPEN 回程代理

真实 OPEN 票不能由这些 API 检索。`open_return` 仅扫描一组回程日期并返回最便宜的候选：

```yaml
- id: hkg-syd-open-proxy
  strategy: open_return
  origin: HKG
  destination: SYD
  depart: 2027-04-01
  return_window: { from: 2027-04-07, to: 2027-04-21, step_days: 3 }
```

### 拆票自转

```yaml
- id: split-example
  strategy: split_ticket
  legs:
    - { from: HKG, to: ICN, date: 2027-04-01 }
    - { from: ICN, to: LIM, date: 2027-04-02 }
```

拆票无误机保护、通常不能直挂行李，可能需要入境与重新托运。保留充足缓冲并自行确认过境要求。

### 可选低价出发枢纽

`hub_open_jaw_scout` 默认只搜索实际 `home_origin` 的开口程。`positioning_hubs` 是可选字段；不配置时不会产生接驳票查询。

```yaml
- id: south-america-hub
  strategy: hub_open_jaw_scout
  home_origin: HKG
  # positioning_hubs: [ICN]  # 可选：启用首尔仁川的接驳票组合
  outbound_destination: LIM
  inbound_origin: GIG
  depart_window:
    from: 2027-04-01
    to: 2027-06-24
    step_days: 14
    max_probes: 60
  trip_days: 27
  positioning: { min_buffer_minutes: 240 }
```

- 仅 HKG：每个日期锚点 1 次查询。
- 加 `positioning_hubs: [ICN]`：每个锚点增加 4 次，分别搜索 `HKG→ICN`、`ICN→LIM`、`GIG→ICN`、`ICN→HKG`。
- 仅当去程和返程在枢纽都满足 `min_buffer_minutes` 时，才会合并成候选。
- 组合报告会展示主程完整往返日期、PNR 数、主程/接驳票小计、实际缓冲与各组件航段。
- 缓冲只是筛选条件，不提供误机保护；独立票行李、入境/过境、改签和航司条款均由旅行者自行确认。

## 行李、签证与过境核验

PDF 候选卡片以中文显示主程往返日期、总时长、逐次中转机场与停留时间、托运行李、签证/过境核验状态、独立 PNR 风险及预订链接。

### 托运行李

- `含 N 件托运行李`：provider 返回明确数量。
- `可能包含托运行李，须以出票页确认`：provider 仅返回模糊文本。
- `未返回托运行李信息`：结果没有行李字段。

工具不根据舱等、航司或经验推断行李额度；尤其是拆票与接驳票，请逐张票确认。

当报价没有行李字段时，PDF 会在独立的“最低经济舱公开参考”行提供官网基础
票价品牌的行李规则，绝不将它写成该报价的已含权益：

| 航司 | 最低经济舱公开参考 |
|---|---|
| Qatar Airways (`QR`) | Economy Lite：美洲航线参考托运 2 件×23kg；手提 1 件≤7kg |
| LATAM (`LA`) | Basic：通常不含托运行李和 12kg 手提大件，仅保留小型随身物品额度 |
| Iberia (`IB`) | Basic：通常不含托运行李；手提 1 件≤10kg，另可带 1 件个人物品 |
| Cathay Pacific (`CX`) | Economy Light：托运 1 件≤23kg；手提 1 件≤7kg |

各候选会附对应官方页面 URL。票价品牌、最主要承运人、联程协议或出票渠道均可能
改变最终额度，必须以出票页列出的 `baggage allowance` 为准。

### 中转机场与建议预留

PDF 的每个航段会展示“中文机场名（IATA）”、实际停留时间与保守规划建议。实际
停留由前一段到达时间和下一段起飞时间计算；建议时间只帮助识别偏紧的行程，
**不是机场最低转机时间（MCT），也不是误机保障**。

| 中转机场 | 保守建议 | 适用说明 |
|---|---:|---|
| 多哈哈马德国际机场（DOH） | ≥2 小时 | 同一票号转机的保守余量 |
| 纽约肯尼迪国际机场（JFK） | ≥3 小时 | 美国首入境：入境、取行李、海关与重新托运 |
| 洛杉矶国际机场（LAX） | ≥3 小时 | 美国首入境：入境、取行李、海关与重新托运 |
| 马德里-巴拉哈斯机场（MAD） | ≥2 小时 | 为跨航站楼、安检或边检留出余量 |
| 巴黎戴高乐机场（CDG） | ≥2 小时 | 为安检、航站楼与短转机排队留出余量 |
| 阿姆斯特丹史基浦机场（AMS） | ≥2 小时 | 为安检或护照检查排队留出余量 |

每条建议在 PDF 中附公开来源链接。更新此映射时应在交互式会话中通过 WebSearch
核实机场、航司或入境机构来源；定时任务只读取本地映射，不做 WebSearch。

### 中国大陆普通护照的官方核验缓存

默认配置以中国大陆普通护照为准：

```yaml
traveler:
  passport_nationality: CN
visa:
  enabled: true
  cache_file: ~/.flight-fare-scanner/visa-cache.json
  cache_max_age_hours: 720
```

交互式真实巡检完成候选航段后，应通过 WebSearch 查找目的地与中转国家的官方移民局、外交部或使领馆页面，再把人工核验摘要写入 `visa.entries`：

```yaml
visa:
  entries:
    - passport_nationality: CN
      country_code: PE
      kind: destination          # destination | transit
      checked_at: 2026-09-01T12:00:00
      source_url: https://官方来源.example/visa
      summary: 根据官方页面整理的核验摘要；出行前仍须复核
```

每个条目都必须有官方来源 URL 与核验时间。工具不会自行判断是否可入境或过境；PDF 始终提示以官方最新规则和承运航司要求为准。定时任务不执行 WebSearch，只读取缓存并标注“引用缓存，非实时”；缺缓存或过期时显示“未完成官方核验”。

## 报告、历史与告警

每个成功运行都会写入：

- PDF：面向阅读，包含主程往返日期、航线、候选票、筛选策略、风险与告警。
- JSON：完整审计数据，适合二次分析。
- SQLite：每个 watch 的价格快照与提醒记录。

告警规则位于 `alerting:`：

```yaml
alerting:
  drop_abs: 300
  drop_pct: 8
  alert_on_new_low: true
  alert_on_rise_pct: 20
  quiet_repeat_hours: 6
```

告警类型：

- `baseline`：首次记录。
- `price_drop`：相对上一轮达到降价阈值。
- `new_low`：低于历史最低价。
- `target_hit`：不高于 `target_price`。
- `market_low`：Google Flights 标为市场低位。
- `price_rise`：涨幅达到预警阈值。

报告路径示例：

```text
~/.flight-fare-scanner/reports/fare-watch-YYYY-MM-DD-HHMMSS.pdf
~/.flight-fare-scanner/reports/fare-watch-YYYY-MM-DD-HHMMSS.json
```

## 邮件通知

配置 SMTP：

```yaml
notify:
  email:
    enabled: true
    host: smtp.gmail.com
    port: 587
    security: starttls       # starttls / ssl / none
    user_env: SMTP_USER
    password_env: SMTP_PASSWORD
    from: your@example.com
    to: [recipient@example.com]
    only_on_alert: true
```

邮件包含纯文本与 HTML 表格，并附上本轮 PDF。`--no-notify` 跳过邮件与钉钉；`--email` 可强制本轮发送。建议使用服务商的应用专用密码。

## 定时巡检

先完成 `validate`，再安装任务。每个 watch 独立调度：

```bash
S=$CODEX_HOME/skills/flight-fare-scanner/scripts

# 每周日期发现
"$S/install_schedule.sh" --watch hk-prd-lon-scout --interval 7d

# 每日关注一个已发现的最优日期组合
"$S/install_schedule.sh" --watch hk-prd-lon-focus --interval 1d

# 删除单独任务
"$S/install_schedule.sh" --uninstall --watch hk-prd-lon-focus
```

macOS 使用 launchd，其他系统使用 crontab。任务启动器会自动加载 `credentials.env`。日志位于：

```text
~/.flight-fare-scanner/watch-<watch-id>.log
```

不要对高调用量策略（日期扫描、拆票、枢纽比价）直接设置高频任务。先观察 `validate` 的 30 天调用估算。

## 额度与调用量

SerpApi 免费档通常为 100 次/月。常见成本：

| 策略 | 每次执行调用量 |
|---|---:|
| `oneway` / `roundtrip` / `multi_city` | 1 |
| `flex_roundtrip` | 日期锚点数 × 游程长度选项数 |
| `focus_roundtrip` | 1 |
| `low_fare_discovery` | 1 |
| `open_return` | 回程候选日期数 |
| `split_ticket` | 票数 |
| `hub_open_jaw_scout` | 锚点数 × (1 + 4 × 可选枢纽数) |

`provider.resolve_return: true` 可能使普通往返额外增加一次查询。使用 `validate` 检查每轮上限和已配置任务的 30 天估算。

## 常见问题与排障

### `Google Flights hasn't returned any results`

按顺序排查：

1. 运行 `validate --show-body` 检查实际机场、日期与筛选。
2. 暂时放宽 `airlines`、`max_stops`、`travel_class`、`max_price`。
3. 对大城市优先使用具体机场。例如伦敦使用 `LHR` 加 `nearby_destinations: [LGW]`，不要依赖 `LON`。
4. 检查是否超过 365 天售票窗口。
5. 对日期扫描，某一锚点失败不代表其他日期无票。

### `hub_open_jaw_scout` 没有枢纽候选

这不代表没有国际票，常见原因是：

- 四张独立票中存在无结果航段；
- 到达/出发时间未满足 `min_buffer_minutes`；
- 白名单或最大中转限制排除了接驳票；
- 远期时刻表尚未发布。

直接 HKG 候选仍会保留。不要降低缓冲后把不可靠组合当作可无风险购买的联程。

### PDF 生成失败

确认 ReportLab 可用：

```bash
python3 -c 'import reportlab; print(reportlab.Version)'
```

若缺失：

```bash
python3 -m pip install reportlab
```

### JSON 无法解析

进度输出在 stderr；只接收 stdout：

```bash
python3 "$S/fare_watch.py" run --config "$CFG" --format json 2>/dev/null | jq .
```

## 安全与合规边界

- 不自动购票、不保存支付信息。
- 不对 ExpertFlyer 等禁止自动采集的服务做爬虫或定时抓取。
- 不将 API Key、密码或 webhook 写入配置、报告或版本控制。
- 本工具只做价格和可用性信息整理；签证、过境、行李、独立 PNR 和航司票规需由旅行者确认。
- API 结果可能延迟、过期或与实际出票页不同；以最终出票页面为准。

## 深入资料

- [SKILL.md](SKILL.md)：AI 使用时的操作规范与需求澄清流程。
- [reference.md](reference.md)：provider 参数、能力契约、输出与调度细节。
- [config.example.yaml](config.example.yaml)：可复制的完整示例。

