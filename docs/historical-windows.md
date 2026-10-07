# 连续全账户窗口报告

`report_windows(data, result, windows)` 接受已执行的显式历史账户输入与连续结果。
`windows` 是 `{名称: {"start": 首个计入交易日, "end": 最后计入交易日}}`，由上层按
原协议解析 development、validation、confirmation、year 与 full；本模块不缩短协议，
不自行替换无数据日期。每个窗口从首日紧邻的前一 session NAV 开始，保留真实持仓、
钱包和费用状态，不另建账户或假设免费清仓。

输入先通过账户 contract 和独立 Fraction replay。报告绑定完整输入、原始结果、
窗口声明、连续日历与边界快照哈希，并保持 `historical_market` 或 `invented_control`。
输出始终为账户诊断，`historical_engine_ready=false`。有限虚构测试不构成完整历史研究。
人民币现金背景另外以独立 Fraction 利息 oracle 核对 funding、完整日历日 accrual
和快照，拒绝删事件、改 credit 或给背景 NAV 凭空加收益。

`windows[name].currencies.CNY/USD` 各包含可单独复算的 performance ledger 与 metrics。
USD 是全账户 CNY NAV 除以该日估值汇率：未兑换的人民币也计入，不能只报告 USD 袖套。
USD risk-free 仍来自同一未兑换人民币背景账户，按各日汇率换算后再计算收益；不会凭空
赠送美元存款收益。收益含既有费用，仅披露而不再次扣除；入场 FX 成本只属于包含首次
执行日的窗口，其余成交和成本按计入交易日筛选。终点仍为持仓估值。

每期 CNY 变动精确分解为：

```
ΔCNYNAV = ΔDomesticCNY + priorFX × ΔUSDAssets
        + priorUSDAssets × ΔFX + ΔUSDAssets × ΔFX
```

USDAssets 采用已复算快照的 `usd_total_equity`，包括证券、现金、应收、未付息及负债的
净值；DomesticCNY 为全账户净 CNY NAV 减去 FX × 净 USDAssets。不得从 gross settled
cash 重构并忽略负债。各项以 exact fraction 保存。净值披露中的舍入残差显式保留，
不当作收益；普通 USD 指标展示采用 50 位十进制有效数字。

`execution_diagnostics` 包括取消原因、事件数量、请求/成交股数、实际日收盘股票占全
账户 CNY NAV 的 session 算术平均，以及正向目标不足配置。后者包括股数取整、现金、
容量约束和价格漂移，不单独归因为下单故障。摩擦与分红预扣税另外披露，已包含在 NAV。

`compare_windows(strategy_report, br_report)` 要求相同冻结上下文、完整日历与窗口名称，
重算报告中的指标，拒绝静默相交日期。各窗口、各币种给出 S 对 BR 的日收益 beta，及
`abs(volBR-volS)/volS <= 0.10` 风险匹配标记。窗口前实际 NAV 可不同，不重置为等本金。
S 风险为零/缺失或收益不足时不标记匹配；BR 方差为零时 beta 明确 undefined。短窗口
CAGR、Calmar 等不可定义项保留原因与 `PARTIALLY_DEFINED`，不能当作完整有效指标。

完整连续 ledger 及其 bankruptcy history 同时绑定哈希。窗口边界或更早已破产时，
状态为 `INHERITED_INSOLVENCY`：保留负 NAV、完整日期、资金流、费用和实际资产变动，
收益、波动、Sharpe、CAGR、Calmar、drawdown 和 beta 明确 undefined。即使持仓升值
使后续边界 NAV 回正，也不恢复收益统计。费用披露使用真实连续前缀及该窗费用行，
不编造正期初值。比较时从绑定的完整 ledger 与历史重新生成窗口状态和指标，拒绝
删除破产记录、改窗口状态或改负期初值的报告。

有限控制验证命令（需已有 Vibe 运行环境）：

```sh
/path/to/existing-vibe/bin/python -B -m unittest tests.test_historical_windows -v
```

控制来自实际安装 Vibe 的虚构连续账本：全账户 USD 已知答案、FX 四项分解、费用只
归属首窗、股息到账仅改变现金形态、beta=1 阳性对照、晚窗不改变早窗，以及缺边界、
缺快照、改 NAV、改指标和改比较日历的拒绝。无需市场下载或真实资金。

v2 控制另在首日实际支付 CNY 70、确认 USD 5 未付负债：CNY 净值 13895、净 USD
总权益 995，首期变动为 domestic -7070 与 prior-FX USD 6965，合计 -105。
后续窗口沿用 13895 边界，不恢复初始 14000，也不重复列入已确认费用；未投资 CNY
现金背景仍为 14000 并独立复算。负债不能作为可交易现金。

小矩阵测试构造入口是 `tests.study_fixture.write_study_fixture(root)`，返回文件 manifest。
它先通过 converter 归一化虚构四 ETF 日线和完整月 inventory，再写八个同源 v2 模型。
范围仅为 `INVENTED_SMALL_CONTROL`：Jan–Oct2020 完整暖场、Nov2020 开发、Dec2020
验证、Jan2021 确认，另有 Feb2021 后续 session。开发 S 严格等于均线而全现金，
每日 FX 变化提供非零风险；十一月上涨使十二月持股。各模型明确非零 CNY/USD 固定
费用，所有 exit-forward 输入为 null，因此不能用这个 fixture 证明现金清仓可实现。
