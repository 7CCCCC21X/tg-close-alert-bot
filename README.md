# TG 合约昨收偏离提醒机器人

监控截图里的四个币安合约：**UNITREEUSDT、HK0625USDT、CXMTUSDT、SKHYNIXUSDT**。

最新成交价相对参考收盘价**上涨超过 1%，或者下跌超过 1%**，向 Telegram 私聊或指定群组话题推送提醒。代码存放 GitHub，Railway 常驻运行。仅监控，不会自动交易、下单或撤单，也不需要交易所 API Key、钱包或私钥。

## 先确认“上一日收盘价”的口径

**本版本默认自动获取币安合约上一完整 UTC 日的日 K 收盘价，不是股票交易所的正式昨收。**

- 当前价：币安合约最新成交价，不是标记价或指数价。
- 自动参考价：币安 `/fapi/v1/klines` 返回的上一完整 UTC 日日 K 的 `Close`。
- UTC 00:00 相当于北京时间 08:00，因此自动基准在北京时间每天 08:00 切换。
- 不是币安首页的滚动 24 小时涨跌幅，也不是北京时间昨天 00:00～24:00 的日线。
- 周末仍按合约自然日取日 K，不把周末当成股票交易日。
- **未自动接入上交所、港交所、韩国交易所的股票官方昨收，也未自动换汇。**

例如，北京时间 2026-09-18 15:00，日 K 模式取的是 UTC 2026-09-17 的完整日 K，结束于北京时间 2026-09-18 07:59:59.999；它不是 9 月 17 日下午股票交易所收盘时的股票价格。

若用途是 Predict 的股票“今日收盘比上个交易日收盘涨/跌”盘，不能直接把本机器人的默认基准当成该盘的结算基准。合约价格、股票原币种报价、汇率和市场指定结算数据源，必须分别核对。

程序另外提供**手动同口径参考价**模式。它只用你提供的价格做比较，不声称该价格已经核验为股票官方昨收，不自动进行币种、汇率或合约倍率换算。

## 默认提醒行为

计算公式：

```text
涨跌百分比 = (当前成交价 - 参考收盘价) / 参考收盘价 × 100
触发条件 = abs(涨跌百分比) > 1
```

假设参考价为 100：101.01 或 98.99 会触发；101、99、100 不触发。这里是严格“大于 1%”，不是“大于等于 1%”。

| 项目 | 默认行为 |
| --- | --- |
| 检查间隔 | 5 秒；网络请求、Telegram 排队会增加实际延迟 |
| 首次采样已超过阈值 | 提醒，无需等下次穿越 |
| 持续超过阈值 | 每 300 秒再次提醒 |
| 偏离继续扩大 | 从超过 1% 到 2%、3% 等更高档位时再次提醒 |
| 同一合约最短通知间隔 | 30 秒，避免价格来回震荡刷屏 |
| 重新触发 | 回到阈值的 80% 内后重新武装；默认为 ±0.8% 内 |
| 涨跌反转 | 反方向超过 1% 时提醒，仍遵守 30 秒最短间隔 |
| 基准换日 | 自动重新计算；新日 K 不可用时不沿用旧基准 |
| 最新成交价过期 | 超过 120 秒未更新，暂停该项涨跌提醒并报告异常 |
| 网络故障/合约缺失 | 不生成假价格、不换成别的合约、不把数据缺失当作未触发 |
| 错误提醒去重 | 同一订阅同一数据项持续异常，最多每 30 分钟提示一次 |
| 状态持久化 | SQLite 保存订阅、话题、设置和已发送提醒，需要挂载 Volume |

5 秒轮询不能保证捕获两次采样之间瞬间发生又恢复的价格波动。这不是实时交易执行系统。HTTP 超时导致发送结果不确定，或者 Telegram 已接收后进程恰好在保存状态之前退出，仍可能出现偶发重复；不承诺分布式“恰好一次”投递。

## GitHub + Railway 部署

### 1. 创建专用 Telegram 机器人

在 Telegram 找到官方 **@BotFather**，发送 `/newbot`，按提示创建机器人并取得 Token。请给本项目使用独立机器人，不要和现有服务共用同一个 Token。

Token 只填在 Railway 的 **Variables**，不要提交到 GitHub，也不要发给别人。

### 2. 上传 GitHub

解压代码包。在 GitHub 新建一个私有仓库，例如 `tg-close-alert-bot`。

通过 **Add file → Upload files** 把解压后的文件和 `tests` 文件夹上传到仓库根目录。不要只上传 ZIP，也不要额外套一层目录。根目录应能直接看到 `main.py`、`Dockerfile`、`railway.json`。

```text
tg-close-alert-bot/
├── main.py
├── Dockerfile
├── railway.json
├── requirements.txt
├── .gitignore
├── .dockerignore
├── .env.example
├── railway-variables.example.txt
├── README.md
├── TESTING.md
└── tests/
    └── test_bot.py
```

`Dockerfile` 会在构建时运行离线测试，再启动机器人。不需要在电脑上安装 Python，也不需要额外搭建数据库服务。

### 3. Railway 连接仓库

在 Railway 创建项目，选择 **Deploy from GitHub repo**，授权并选择刚才的仓库。先添加变量，再正式部署；若平台已经自动开始构建，补好变量后重新部署即可。

使用代码包中的 `Dockerfile` 和 `railway.json`，启动命令已配置为：

```text
python -u main.py
```

这是常驻 worker，不是网站：**不需要添加域名、不需要 PORT、不要设置 /health 健康检查、不要配置 Cron。**

在服务设置中保持**单个部署区域、1 个副本**；关闭服务休眠/Serverless。不要同时在本地、预览环境和另一个 Railway 服务运行同一个 Token。运行环境应符合 Binance、Telegram、Railway 的地区及服务条款；不要通过非官方接口或关闭证书验证来绕过限制。

### 4. Variables 填写

在 **Variables → Raw Editor** 中粘贴下面内容。先只替换 Token；用户 ID 不知道时保留 0。

```env
TELEGRAM_BOT_TOKEN=替换成BotFather给你的Token
ADMIN_USER_ID=0
SYMBOLS=UNITREEUSDT,HK0625USDT,CXMTUSDT,SKHYNIXUSDT
ALERT_THRESHOLD_PCT=1
POLL_SECONDS=5
ALERT_COOLDOWN_SECONDS=300
ALERT_STEP_PCT=1
MIN_ALERT_GAP_SECONDS=30
MAX_PRICE_AGE_SECONDS=120
BASELINE_MODE=binance_daily
STATE_DB=/data/bot.sqlite3
```

注意 `ALERT_THRESHOLD_PCT=1` 就是 **1%**，不是填 `0.01`。

`ADMIN_USER_ID` 是你本人的**数字用户 ID**，不是用户名、手机号、机器人 ID、群 ID 或话题 ID。没有设置管理员时，程序只允许查询 `/id`，不会让第一个陌生用户自动接管机器人。

`TELEGRAM_CHAT_ID` 不需要填写。程序不根据环境变量自动创建订阅；实际发到哪里，由管理员在那个私聊/群组话题里发送 `/subscribe` 决定。

### 5. 挂载持久磁盘

在 Railway 给**这个机器人服务**添加一个 **Volume**，挂载目录填写：

```text
/data
```

变量 `STATE_DB` 使用 `/data/bot.sqlite3`。不要把挂载路径写成文件名。

**一定要完成这一步。** 没有 Volume，容器重新部署后订阅、手动参考价、提醒去重记录和 TG 修改的设置可能丢失。磁盘本身不是自动创建的，`railway.json` 不能代替你添加 Volume。

### 6. 绑定管理员，开始订阅

先部署成功。在你的机器人私聊里发送：

```text
/id
```

机器人会回复你的用户 ID。把数字填回 Railway 的 `ADMIN_USER_ID`，保存并重新部署。

重新部署后，在机器人私聊发送：

```text
/subscribe
/status
```

你应当看到四个合约的当前成交价、基准价、相对基准涨跌和行情时间。**只有 `/status` 中各项数据有效，才说明行情链路也在工作。** 单独收到 `/test` 并不代表已订阅，也不代表行情接口正常。

### 7. 推送到群组话题

把机器人加入目标群，确认它可以发送消息。使用管理员本人账号进入目标话题，在**话题内部**发送：

```text
/subscribe@你的机器人用户名
```

程序同时保存 `chat_id` 和 `message_thread_id`，后续通知会发回该话题，不会只发到群组默认页面。

用同样方式可以在另一个话题订阅。各个订阅分别保存提醒状态。`/pause`、`/resume`、`/unsubscribe` 只作用于发命令的当前私聊/话题；`/threshold`、`/cooldown`、`/mode` 则修改所有订阅使用的全局设置。

## Telegram 命令

机器人启动时会自动向 Telegram 注册命令列表（`setMyCommands`），输入框左下角会出现 **“菜单”** 按钮，点开即可直接选择下面这些命令，不必手动输入。注册失败只会记录一条警告，手动输入命令仍然可用。

| 命令 | 作用 |
| --- | --- |
| `/subscribe` | 订阅当前私聊或当前群话题 |
| `/unsubscribe` | 取消当前订阅并删除其去重状态 |
| `/status` 或 `/price` | 查询四个合约的行情、基准和错误状态 |
| `/threshold 1` | 设为严格超过 ±1% 提醒 |
| `/cooldown 300` | 持续超过阈值，每 300 秒提醒 |
| `/cooldown 0` | 关闭周期重复提醒，仍保留首次、重新触发及扩大档位提醒 |
| `/mode daily` | 自动币安上一完整 UTC 日日 K 模式 |
| `/mode manual` | 手动参考价模式 |
| `/setclose UNITREE 75` | 为北京时间今天设置该合约参考价 75 |
| `/setclose UNITREE 75 2026-09-18` | 为指定适用日期设置参考价，日期不是收盘发生日 |
| `/pause` | 暂停当前订阅 |
| `/resume` | 恢复当前订阅 |
| `/test` | 仅测试消息能否发到当前私聊/话题 |
| `/id` | 显示自己的用户 ID、当前聊天 ID、话题 ID |
| `/help` | 查看帮助 |

## 手动参考价模式

它适合已经核对参考价格、需要让机器人围绕这条固定价格线提醒的场景。

以下为**虚构示例，不是实际昨收价**：

```text
/mode manual
/setclose UNITREE 75
/setclose HK0625 40
/setclose CXMT 8
/setclose SKHYNIX 1300
/status
```

省略日期时默认适用北京时间今天。也可以提前给次日设置；程序按适用日期分别保存，不覆盖今天的价格。

**价格必须和币安合约显示数值采用相同的计价口径。** 不要直接把韩国股票的韩元收盘数字与另一个币种的合约价格比较。币安界面的 USDT 后缀或美元显示也不能代替对具体合约计价规则、兑换方式、倍率的核验。本程序不会替你做这些换算。

手动参考价到该适用日北京时间 24:00 失效。下一天缺少新的参考价时，相关合约暂停涨跌提醒并提示缺失；不会把过期参考价一直当作“昨日收盘”。周末或假期需要继续使用同一个参考价，也必须明确设置对应适用日期。

切换回自动模式：

```text
/mode daily
```

## 可调整的变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | 无 | 必填，机器人 Token |
| `ADMIN_USER_ID` | `0` | 管理员数字用户 ID，0 为仅允许获取 ID 的初始化状态 |
| `SYMBOLS` | 截图中的四个合约 | 英文逗号分隔，精确使用币安代码 |
| `ALERT_THRESHOLD_PCT` | `1` | 百分数，1 代表 1% |
| `POLL_SECONDS` | `5` | 行情轮询目标间隔，最小 3 秒 |
| `ALERT_COOLDOWN_SECONDS` | `300` | 持续超标的周期提醒间隔，0 关闭 |
| `ALERT_STEP_PCT` | `1` | 超过阈值后每扩大多少个百分点进入新提醒档位，0 关闭 |
| `MIN_ALERT_GAP_SECONDS` | `30` | 同一订阅同一合约两次提醒的最短间隔 |
| `MAX_PRICE_AGE_SECONDS` | `120` | 最新成交时间戳允许的最大年龄 |
| `BASELINE_MODE` | `binance_daily` | `binance_daily` 或 `manual` |
| `STATE_DB` | 本地 `./data/bot.sqlite3`；Docker `/data/bot.sqlite3` | SQLite 文件位置 |

通过 TG 命令修改的阈值、冷却和模式会保存到 Volume，并**优先于对应环境变量默认值**。以后要修改它们，直接发 TG 命令；只改 Railway 对应默认变量不一定覆盖已有持久设置。

自动模式每一轮重新抓最新价；同一天内的上一完整日 K 会缓存，不会每 5 秒重复获取。每天换日时重新抓取，缺失时拒绝用旧日 K 代替。

## 日志与排错

**部署报 Token 错误：** 检查变量名和完整 Token，不要只填机器人的用户名。Token 不要包含额外引号或换行。

**`ADMIN_USER_ID 尚未配置`：** 先私聊机器人发送 `/id`，把返回的用户 ID 填入变量并重新部署。

**没有收到价格提醒：** 先发 `/subscribe`，再 `/status`。没有订阅、当前订阅暂停、价格没有严格超过阈值、冷却期内、基准或行情无效，均不会发涨跌提醒。

**`HTTP 409`：** 同一 Token 在多个地方运行或仍有旧实例。停止其他实例；副本数设为 1。不要同时让本地程序和 Railway 用同一个 Token 轮询。

**已有 Webhook：** 本程序不会擅自删除原服务的 Webhook。改用专用机器人，或者先在原服务中移除 Webhook 再启动。

**`HTTP 451/403`：** 服务地区、权限或访问规则不允许该请求。核对各平台官方规则、可用服务地区和部署配置。程序不会自动尝试绕过限制。

**`HTTP 429/418`：** 限流或临时封禁。程序会按返回等待时间暂停币安请求，避免继续高频请求。

**合约不存在：** 查看 `SYMBOLS` 和 `/status` 的错误。程序不会把 `HK0625USDT` 猜成其他股票或交易对。

**“最新成交价已过期”：** 接口里最新一笔成交的时间戳超过 120 秒，不把它当作即时价格。低成交活跃度也可能触发这种保护，不一定说明连接中断。

**重启后丢订阅：** 检查 Volume 是否挂在当前服务的 `/data`，以及 `STATE_DB` 是否为 `/data/bot.sqlite3`。

**构建成功但没有网站：** 正常，这是后台 worker，不提供网页，不需要添加域名。

每分钟日志会打印：

```text
heartbeat: valid_quotes=4/4 active_subscriptions=1 mode=binance_daily
```

日志中的 valid_quotes 是最近完成采样时的有效项计数，不等同于持续健康保证。以 `/status` 中的行情时间和基准日期一起核对。

## 本地测试与行情诊断

Python 3.12 环境下运行离线测试，无须 Token，无须联网：

```bash
python -m unittest discover -s tests -v
```

只测试真实币安接口，不推送 Telegram：

```bash
python main.py --check
```

`--check` 固定诊断自动日 K 模式，逐个输出最新价、上一完整日 K、偏离值；失败返回非零退出码。当前生成环境无法联网完成此真实接口测试，见 `TESTING.md`。

本地启动机器人时要把配置写入操作系统环境变量；本项目不会自动读取 `.env` 文件。使用 Docker 时可以让 Docker 读取自己保存的私有 `.env`：

```bash
docker build -t tg-close-alert-bot .
docker run --rm --env-file .env -v "$(pwd)/data:/data" tg-close-alert-bot
```

仅使用一个运行实例。本地测试真实 Bot 时应先停止 Railway 同 Token 的实例。

## 接口与部署文档

开发时核对的官方资料（接口与平台可能后续变更）：

- Binance USDⓈ-M Market Data：`https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data`
- Telegram Bot API：`https://core.telegram.org/bots/api`
- Telegram BotFather：`https://core.telegram.org/bots/features#botfather`
- Railway GitHub 部署：`https://docs.railway.com/quick-start`
- Railway Dockerfile：`https://docs.railway.com/builds/dockerfiles`
- Railway 配置文件：`https://docs.railway.com/config-as-code/reference`
- Railway Volume：`https://docs.railway.com/volumes`

本程序使用的行情接口仅有 `/fapi/v1/time`、`/fapi/v2/ticker/price`、`/fapi/v1/klines`，没有任何交易或账户操作接口。Telegram 使用 `getMe`、`getWebhookInfo`、`getUpdates`、`sendMessage`。
