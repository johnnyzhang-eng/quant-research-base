# 四只 ETF 的历史数据入口核对

2026-10-07 核对了 EODHD 与 Alpha Vantage 的官方产品、数据说明和条款。目标仍是 SPY、EFA、IEF、GLD 从 2005 年开始的历史，尚未取得或验收完整数据。

| 候选 | 已核对的产品与条件 | 尚待闭合的关键问题 |
| --- | --- | --- |
| EODHD Historian | 官网列出的北美地区月费基础报价 USD 19.99，另有 EUR/GBP 地区币种报价；符合 Non-Professional 定义的个人非商业自有投资用途才适用相应保存、处理和分析许可。 | 实际地区价格与税费；退订及合同终止后的留存；四只 ETF 的实际完整历史；股息支付日完整性；自有账户自动化模型使用范围。 |
| Alpha Vantage | 完整 daily 历史需要 premium；最低月付表单显示 $49.99，币种代码及最终账单未独立确认。 | 原始及转换数据的长期保存范围；四只 ETF 的实际覆盖、历史支付日及模型使用权限。 |

两家均须按真实用途核对个人／商业及金融专业分类；尚未确认这些个人许可适用于实际使用者。EODHD 个人范围不包括代表他人或组织使用。

EODHD 文档中的 volume 是拆股调整值。若继续要求原始 OHLCV，需要核对原始成交量入口或可验证的完整还原方法。JSON dividends 有 paymentDate 字段，但允许缺值；字段存在不能代替四只 ETF 从 2005 年开始的逐条现金到账核对。定价与有效条款分别见 [官方定价](https://eodhd.com/pricing)、[使用条款](https://eodhd.com/financial-apis/terms-conditions)、[EOD 数据说明](https://eodhd.com/financial-apis/api-for-historical-data-and-volumes) 和 [分红拆股说明](https://eodhd.com/financial-apis/api-splits-dividends)。

Alpha Vantage 的产品和范围分别见 [premium](https://www.alphavantage.co/premium/)、[官方文档](https://www.alphavantage.co/documentation/) 和 [服务条款](https://www.alphavantage.co/terms_of_service/)。产品宣传的历史长度不能证明四个指定标的均完整；未发现删除条款也不能推断永久保存许可。

已准备一份供应商询问草稿，尚未发送、收到回复或购买。每个候选的 12 次请求方案仅为设计，本次没有市场 API 请求、密钥、账户或订单操作。原始数据协议与策略参数保持原样；这份产品核对不证明投资收益。
