# 有限的实际 Vibe 账户控制

`historical_account_controls.run_controls(output_dir)` 使用已安装的
`GlobalEquityEngine` 执行十个有限虚构案例，不注入替代引擎、不下载数据。
目标是核验显式账户模型接入、实际事件账本与独立 Fraction 复算的对应关系。
报告分类固定为 `INVENTED_INTEGRATED_ACCOUNT_CONTROLS_ONLY`，输入保留
`invented_control`，`historical_engine_ready=false`、`formal_history_runs=0`。
这不是完整 S02 历史矩阵、历史收益研究或券商模拟成交证明。

基准账户明确声明初始人民币 14000、兑换本金 7000、汇率 7，股票初始为零。
各 ETF 原始价格 100、成交量 1000000、显式开盘容量 10000 股；手续费、利息、
摩擦、兑换成本显式为零。冻结的全账户 USD 目标权益为 2000，四只 ETF 的目标
各为五股；实际首日只能使用已兑换的 USD 1000，依次买入 SPY 与 EFA 各五股。
虚构工作日日历和定价、规则、时钟只用于控制，不声明实际交易所或银行事实。

| 案例 | 冻结答案与所检机制 |
| --- | --- |
| baseline | 首日目标各 5 股；实际买 SPY/EFA 各 5；末值 CNY 14000 |
| fx_move | 入场汇率 7、收盘汇率 8；早先目标仍各 5，首日 NAV 15000 |
| c3 | 额外延后一 session 到 10/2；原始开盘 200，10bp 摩擦后 200.2，新规则最低费 2，T+1 于 10/5 结算 |
| dividend | 10/16 毛分红 1、预扣 20%，净应收 4 在到账前不可花；10/17 周六付一次，周一现金 4、应收 0、NAV 13993 |
| bc | 无股票成交；首末 CNY 14000；收益指标的值为浮点 0.0 |
| interest | USD 1000、年利率 .365，两天各入账 1；CNY NAV 14007、14014 |
| weekday | 10/19 16:30 付款在 regular close 后；日截止快照现金 4、股票和钱包应收均 0、NAV 13993 |
| monthend | calendar_month_end 在 10/31 入账 USD 31，股票簿 11/2 接收；末现金 1031、未付息 3.09、NAV 14238.63 |
| unknown_100 / unknown_200 | 首日开盘资料晚到；改变尚不可见开盘 100→200，目标和下单前缀相同、各 5 股、首日均无成交 |

每次实际执行都调用 `historical_replay.independent_replay`。该复算独立采用
Fraction，核对全账户 CNY NAV、来源时钟、目标与请求数量、手续费、摩擦、结算、
股息和利息事件。另从实际输出复制三份篡改账本：将首笔成交费改为 999、将 FX
首日 NAV 改为 15001、重复一个真实利息 credit 的来源游标；三者必须被复算拒绝。
变异在内存副本执行，不覆盖原账本或源代码。

`native_fill_reconciliation(result)` 对照实际 Vibe 原生 fill 与股票 FILL 的数量、
符号、方向、股数、日期、价格和费用。空 BC 账本对此比较自然一致；调用者仍须
核验实际引擎源码指纹。原生成交一致不等于另一个引擎独立复算。

报告固定为十个执行案例、32 个 `reports[{id,expected,actual,passed}]`。
`control_report_accepted(report)` 检查独立字面答案 inventory 与类型保真的 canonical
hash，不相信生产者 `accepted`，不接受删行、重复行、自改 expected 或 `0`/`0.0`
类型替代。它核验报告内容；证据文件完整性另由封存 manifest 和上层运行封存验证。

新目录以排他创建保存 source fixture，以及每个案例的 artifact、model、features、
account-input、native-result、fraction-replay、performance-input、metrics；三种变异
也保存原输入/结果哈希与复算错误。末尾 manifest 绑定全部文件字节（自身除外）。
虚构 fixture 的原始文件引用保留；各案例修改后的 artifact 完整保存并由 lineage
hash 绑定，不把未修改的原 CSV 哈希当作修改后数据的证明。

运行需要已有 Vibe 安装，普通环境中的对应测试会显式跳过。使用已有运行环境的验证命令：

```sh
/path/to/existing-vibe/bin/python -B -m unittest tests.test_historical_account_controls -v
```

付款时钟仍是模型声明的截止时刻，开盘价格是有容量和可用性约束的模型锚；
来源权限由实际执行再次检查，但哈希和 reviewed evidence 不认证授权真实性。
