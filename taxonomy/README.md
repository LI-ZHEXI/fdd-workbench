# 标准科目树口径

`std_accounts.csv` 含 67 个标准科目（IS 24、BS 43）；CF 科目在 v0.3 再加入。国内 TB 的结转科目、备抵科目和 debt-like 拆项单列，以减少未映射科目的误报。

1. **D&A 独立且不进 EBITDA。** 折旧、摊销各设独立科目，`is_ebitda_component=FALSE`，避免计入 EBITDA。混在经营费用中的 D&A 后续通过 `reclass_rule` 拆出。
2. **信用减值在 EBITDA 内，资产减值剔除。** 信用减值损失视作经营性成本，`is_ebitda_component=TRUE`。资产减值损失归入 `non_op`，不计入 EBITDA。
3. **应付票据默认属于 NWC。** 应付票据默认归入 `nwc_ap`，并设置 `is_nwc_component=TRUE`。具有融资性质的票据可通过 `reclass_rule` 重分类为 `debt_like`，反映其融资实质。
4. **受限资金不进净债务。** 受限货币资金保留 `category=cash`，但 `is_net_debt_component=FALSE`。其可用性受限制，不能按默认口径直接抵减债务。
5. **结转科目只为保持 TB 平衡。** 本年利润、利润分配映射到 `equity`，三个计算开关全部为 `FALSE`。保留这些科目用于 TB 平衡，避免重复计入经营成果或交易桥梁。
6. **备抵科目与主科目同 category、同 normal_sign。** `normal_sign` 是将借正贷负余额换算为所属大类自然方向的乘数，不是该科目自身的正常余额方向。应收 `+100` 与坏账准备 `-10` 同乘 `+1` 得净额 `90`；若将坏账准备误设为 `-1`，汇总会错误地变成 `110`。

税金及附加与信用减值损失归入 `opex_ga`，因为当前 category 枚举没有它们的专属大类。两者均按经营性项目处理，计入 EBITDA。
