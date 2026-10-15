# 三种收益机制的资料阅读与取舍（2026-10-15）

本轮从已有研究比较股票短期反转、加密carry、公开内部人交易信息，研究资金尺度沿用5—30万元，投资市场尚未锁定。完成的是原文阅读与研究取舍，没有新回测、行情下载、数据购买或资金操作，也没有策略通过净盈利、回撤≤30%或少亏损月验收。

| 路线 | 已读知识 | 当前取舍 |
| --- | --- | --- |
| 短期反转 | 可用流动性提供、库存与逆向信息风险解释。月度STR、日度流动性代理和LSY五片是不同构造；机构成本结论不可统判小账户。 | 保留机制，下一阅读缺口是行业调整、更新样本与长仓端贡献。 |
| 加密carry | 组合保证金净额效率改善后价格偏离缩小，支持部分资本约束解释；简单波动约束代理并未支持预期方向。 | 保留机制；暂不把高资金费APR作为交易规则。 |
| 公开内部人交易信息 | 原研究按历史日历规律区分routine/opportunistic，不是简单的多名内部人同时买入。多空、长仓自身、两类买入差值须区分。 | 优先追查发表后已有复刻及公开披露时点，先读别人做过的实验。 |

## 可核来源与实际阅读边界

[Frazzini、Israel、Moskowitz 2012作者稿](https://pages.stern.nyu.edu/~afrazzin/pdf/Trading%20Cost%20of%20Asset%20Pricing%20Anomalies%20-%20Frazzini,%20Israel%20and%20Moskowitz.pdf)已读构造、成本资料与优化实验相关段。其成本用1998—2011机构交易，不是今日个人账户报价。Nagel全文访问失败，只核实[Stanford机构摘要](https://gsbpreserve.stanford.edu/view/41388/evaporating-liquidity)，未冒称完整精读。

[Fundamentals of Perpetual Futures v7](https://arxiv.org/abs/2212.06888v7)，2026-09修订，但样本截至2024-03-11。实际读引言、费用与数据段、资本约束事件及相反代理证据；没有通读全部证明或复算。Table3的高费用档仍采用maker与百万美元级月成交门槛，不能直接套给本研究资金尺度。到期期货基差与永续资金费不能互换证据。另读[EPFL作者现金流一页说明](https://www.epfl.ch/labs/sfi-jh/wp-content/uploads/2024/10/Perp_price_one_page.pdf)。

[Cohen、Malloy、Pomorski原作者稿](https://dash.harvard.edu/server/api/core/bitstreams/7312037e-2b77-6bd4-e053-0100007fdf3b/content)已读定义、数据、收益窗、TableIV与A7。五因子模型下机会型买入自身alpha为0.72%/月、t=2.27；机会型买入减routine买入的差为0.63%、t=1.61。Carhart下机会型买入自身t=1.73，结论对对象和模型有依赖。这些是历史论文毛证据，不是小账户净收益认证。[SEC官方字段说明](https://www.sec.gov/files/insider_transactions_readme.pdf)的P码还包括私人购买，申报日期与实际公开时刻也不能静默等同。

## 复用已有成果，而不是先造数据库

[JKP作者网站](https://jkpfactors.com/data)提供按主题整理的研究、预计算因子与分组组合入口；[当前Python仓](https://github.com/bkelly-lab/jkp-data)说明底层授权与构建条件。先复用定义、论文和现有资料，避免全量重建。原始股票级数据、当前账户许可与可下载文件没有验收；公开USD因子收益也不是人民币账户净收益。没有安装该仓库或构建全量数据。

## 模型协作的真实结果

Codex承担检索、分工、原文核对和采纳。本轮官方DeepSeek reasoning一次真实终答，建议优先追公开内部人信息，峰值token价估算US$0.0037638，实际账单未核。未配置浏览工具。

其把“买入两类差值不显著”泛化为“买入端自身不显著”，并提出含混的申报日加法定延迟，已依据TableIV/A7纠正；也没有把其任意t阈值当实验门槛。它建议再读的CMP表格本轮已读，因此下一步改为找后续现成研究，而不是重复列同一阅读动作。

WorkBuddy本轮遇原生窗口输入故障，控制服务重置后重新绑定仍未恢复送达，0新请求、0新终答。没有把此前Step答复当本轮参与；这也不是对模型推理能力的失败判定。未追加收费裁决或模型投票。

## 找到别人已经做过的现代样本复刻

已取得[Aalto官方仓储的You Wu硕士论文](https://aaltodoc.aalto.fi/items/bf8fffa7-901a-4aa1-8dcf-56c5b6cacc9b)，题为 *Insider Trading and Information Asymmetry: A Replication and Extension of Cohen, Malloy, and Pomorski (2012)*。实际精读摘要、数据、组合构建、Table4、结论等段，作者报告研究2008—2024的Form4交易，并匹配CRSP/Compustat等资料。

它可以帮助避免从头做宽泛复刻，但不能直接继承结论：收益按交易月份形成，所读方法未清楚对齐真实公开披露时间；一处正文与Table4的组合alpha对应关系需要澄清；尚未核到成本扣除、代码及完整可复现数据。这是未经本项目独立复现的硕士研究，不冒称同行评议定论，更不把作者alpha当可交易净收益。书目日期在封面、摘要、仓储与文件名间有差异，暂不强行合并。

下一知识问题：是否已有发表后研究，使用真实公开时点和可交易长仓定义，仍支持内部人信号的增量？先查现成证据与失败经验，足以决定是否复刻时收敛。当前不因这个缺口自动启动全量申报/行情工程，也不锁定实际投资市场。
