-- Canonical model DDL. Source of truth: README.md §3.2. Keep in sync.
-- 1. 主数据：被分析公司 / 法人实体（v0.4 可扩展可比公司属性）
CREATE TABLE IF NOT EXISTS entity (
    entity_id VARCHAR PRIMARY KEY,
    name VARCHAR NOT NULL,
    market VARCHAR,                         -- 'CN_PRIVATE' | 'A_SHARE' | 'HKEX' | 'US_SEC'
    reporting_currency VARCHAR(10) NOT NULL DEFAULT 'CNY',
    fiscal_year_end VARCHAR(5) NOT NULL DEFAULT '12-31',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. 主数据：报告期与口径边界
CREATE TABLE IF NOT EXISTS period (
    period_id VARCHAR PRIMARY KEY,          -- e.g. 'ENT01_2025M12_MGMT_CONS'
    entity_id VARCHAR NOT NULL REFERENCES entity(entity_id),
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    grain VARCHAR NOT NULL,                 -- 'month' | 'quarter' | 'year'
    basis VARCHAR NOT NULL,                 -- 'audited' | 'management' | 'unaudited'
    consolidation_scope VARCHAR NOT NULL DEFAULT 'consolidated', -- 'consolidated' | 'standalone'
    UNIQUE (entity_id, start_date, end_date, grain, basis, consolidation_scope)
);

-- 3. 主数据：源文件登记与不可变哈希
CREATE TABLE IF NOT EXISTS source_file (
    file_id VARCHAR PRIMARY KEY,
    entity_id VARCHAR NOT NULL REFERENCES entity(entity_id),
    path VARCHAR NOT NULL,
    sha256 VARCHAR(64) NOT NULL UNIQUE,
    kind VARCHAR NOT NULL,                  -- 'tb' | 'gl' | 'ar_aging' | 'ap_aging' | 'bank' | 'statement' | 'sales'
    imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    row_count INTEGER,
    page_count INTEGER,
    notes VARCHAR
);

-- 4. 标准 FDD 科目树
CREATE TABLE IF NOT EXISTS std_account (
    std_code VARCHAR PRIMARY KEY,
    name VARCHAR NOT NULL,
    name_zh VARCHAR,
    statement VARCHAR NOT NULL,             -- 'IS' | 'BS' | 'CF'
    category VARCHAR NOT NULL,              -- 'revenue' | 'cogs' | 'opex_sm' | 'opex_ga' | 'opex_rd' | 'da' | 'interest' | 'tax' | 'non_op' | 'nwc_ar' | 'nwc_inventory' | 'nwc_ap' | 'nwc_other' | 'cash' | 'debt' | 'debt_like' | 'equity' | 'other'
    normal_sign INTEGER NOT NULL,           -- 借正贷负余额换算为所属大类自然方向的乘数: +1 (资产/成本费用) | -1 (负债/权益/收入); 备抵科目与主科目同 category 同 normal_sign
    is_ebitda_component BOOLEAN NOT NULL DEFAULT FALSE,
    is_nwc_component BOOLEAN NOT NULL DEFAULT FALSE,
    is_net_debt_component BOOLEAN NOT NULL DEFAULT FALSE
);

-- 5. 本地科目表与科目级映射
CREATE TABLE IF NOT EXISTS coa_local (
    entity_id VARCHAR NOT NULL REFERENCES entity(entity_id),
    local_code VARCHAR NOT NULL,
    local_name VARCHAR NOT NULL,
    PRIMARY KEY (entity_id, local_code)
);

CREATE TABLE IF NOT EXISTS coa_map (
    map_id VARCHAR PRIMARY KEY,
    entity_id VARCHAR NOT NULL,
    local_code VARCHAR NOT NULL,
    std_code VARCHAR NOT NULL REFERENCES std_account(std_code),
    mapping_version INTEGER NOT NULL DEFAULT 1,
    source VARCHAR NOT NULL DEFAULT 'manual', -- 'manual' | 'rule' | 'llm_confirmed'
    reviewer VARCHAR NOT NULL,
    reviewed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (entity_id, local_code) REFERENCES coa_local(entity_id, local_code),
    UNIQUE (entity_id, local_code, mapping_version)
);

-- 6. 条件重分类规则（优先级高于 coa_map，解决一对多拆账）
CREATE TABLE IF NOT EXISTS reclass_rule (
    rule_id VARCHAR PRIMARY KEY,
    entity_id VARCHAR NOT NULL REFERENCES entity(entity_id),
    priority INTEGER NOT NULL DEFAULT 100,  -- 数字越小优先级越高
    target_table VARCHAR NOT NULL,          -- 'gl_entry' | 'trial_balance' | 'aging_balance'
    match_local_code VARCHAR,
    match_counterparty_regex VARCHAR,
    match_description_regex VARCHAR,
    override_std_code VARCHAR NOT NULL REFERENCES std_account(std_code),
    reason VARCHAR NOT NULL,
    reviewer VARCHAR NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE
);

-- 7. 往来方主数据
CREATE TABLE IF NOT EXISTS counterparty (
    counterparty_id VARCHAR PRIMARY KEY,
    entity_id VARCHAR NOT NULL REFERENCES entity(entity_id),
    name VARCHAR NOT NULL,
    party_type VARCHAR NOT NULL DEFAULT 'unknown', -- 'customer' | 'supplier' | 'employee' | 'shareholder' | 'bank' | 'other' | 'unknown'
    related_party_flag BOOLEAN NOT NULL DEFAULT FALSE,
    evidence_ref VARCHAR
);

-- 8. 事实表：试算平衡表（Trial Balance）
CREATE TABLE IF NOT EXISTS trial_balance (
    tb_id VARCHAR PRIMARY KEY,
    entity_id VARCHAR NOT NULL REFERENCES entity(entity_id),
    period_id VARCHAR NOT NULL REFERENCES period(period_id),
    local_code VARCHAR NOT NULL,
    opening DECIMAL(18, 4) NOT NULL DEFAULT 0,       -- 有符号：借正贷负
    debit DECIMAL(18, 4) NOT NULL DEFAULT 0,         -- 本期借方发生额 (>=0)
    credit DECIMAL(18, 4) NOT NULL DEFAULT 0,        -- 本期贷方发生额 (>=0)
    closing DECIMAL(18, 4) NOT NULL DEFAULT 0,       -- 有符号：借正贷负
    ytd_debit DECIMAL(18, 4),                        -- 本年累计借方（防期末损益结转归零）
    ytd_credit DECIMAL(18, 4),                       -- 本年累计贷方
    is_pre_closing BOOLEAN NOT NULL DEFAULT TRUE,
    raw_currency VARCHAR(10) NOT NULL DEFAULT 'CNY',
    file_id VARCHAR NOT NULL REFERENCES source_file(file_id),
    row_no INTEGER NOT NULL,
    UNIQUE (entity_id, period_id, local_code)
);

-- 9. 事实表：总账分录（GL Entry）
CREATE TABLE IF NOT EXISTS gl_entry (
    gl_id VARCHAR PRIMARY KEY,
    entity_id VARCHAR NOT NULL REFERENCES entity(entity_id),
    period_id VARCHAR NOT NULL REFERENCES period(period_id),
    voucher_no VARCHAR NOT NULL,
    voucher_line_no INTEGER NOT NULL,
    entry_date DATE,
    posting_date DATE NOT NULL,
    local_code VARCHAR NOT NULL,
    debit DECIMAL(18, 4) NOT NULL DEFAULT 0,
    credit DECIMAL(18, 4) NOT NULL DEFAULT 0,
    counterparty_id VARCHAR REFERENCES counterparty(counterparty_id),
    raw_counterparty VARCHAR,
    description VARCHAR,
    raw_currency VARCHAR(10) NOT NULL DEFAULT 'CNY',
    file_id VARCHAR NOT NULL REFERENCES source_file(file_id),
    row_no INTEGER NOT NULL,
    -- 含 period_id 以兼容国内财务软件按月重置凭证号（如每月记-0001）的全年单文件导出；单期文件下等价于 (entity_id, voucher_no, voucher_line_no, file_id)
    UNIQUE (entity_id, period_id, voucher_no, voucher_line_no, file_id)
);

-- 10. 事实表：报表及附注提取行（v0.3 PDF / XBRL 报表勾稽入口）
CREATE TABLE IF NOT EXISTS statement_line (
    stmt_line_id VARCHAR PRIMARY KEY,
    entity_id VARCHAR NOT NULL REFERENCES entity(entity_id),
    period_id VARCHAR NOT NULL REFERENCES period(period_id),
    statement VARCHAR NOT NULL,             -- 'IS' | 'BS' | 'CF' | 'NOTE'
    raw_label VARCHAR NOT NULL,
    std_code VARCHAR REFERENCES std_account(std_code), -- NULL 表示已提取但待映射确认
    amount DECIMAL(18, 4) NOT NULL,
    raw_currency VARCHAR(10) NOT NULL DEFAULT 'CNY',
    extraction_confidence DECIMAL(5, 4) DEFAULT 1.0,
    file_id VARCHAR NOT NULL REFERENCES source_file(file_id),
    page_no INTEGER,
    row_no INTEGER
);

-- 11. 事实表：应收/应付账龄合表（用 ledger_side 区分 AR / AP）
CREATE TABLE IF NOT EXISTS aging_balance (
    aging_id VARCHAR PRIMARY KEY,
    entity_id VARCHAR NOT NULL REFERENCES entity(entity_id),
    period_id VARCHAR NOT NULL REFERENCES period(period_id),
    ledger_side VARCHAR NOT NULL,           -- 'AR' | 'AP'
    as_of DATE NOT NULL,
    counterparty_id VARCHAR REFERENCES counterparty(counterparty_id),
    raw_counterparty VARCHAR NOT NULL,
    bucket VARCHAR NOT NULL,                -- '0-30d' | '31-90d' | '91-180d' | '181-365d' | '>365d'
    amount DECIMAL(18, 4) NOT NULL,
    provision_amount DECIMAL(18, 4) DEFAULT 0,
    raw_currency VARCHAR(10) NOT NULL DEFAULT 'CNY',
    file_id VARCHAR NOT NULL REFERENCES source_file(file_id),
    row_no INTEGER NOT NULL
);

-- 12. 事实表：销售发票/订单明细（v0.4 收入明细核查与扩展分析）
CREATE TABLE IF NOT EXISTS sales_line (
    sales_id VARCHAR PRIMARY KEY,
    entity_id VARCHAR NOT NULL REFERENCES entity(entity_id),
    period_id VARCHAR REFERENCES period(period_id),
    invoice_no VARCHAR NOT NULL,
    invoice_date DATE NOT NULL,
    customer_id VARCHAR REFERENCES counterparty(counterparty_id),
    raw_customer VARCHAR NOT NULL,
    product VARCHAR NOT NULL,
    quantity DECIMAL(18, 4) NOT NULL,
    unit_price DECIMAL(18, 4),
    amount DECIMAL(18, 4) NOT NULL,         -- 净不含税收入额（退货/折让为负）
    gross_margin_amount DECIMAL(18, 4),
    is_credit_note BOOLEAN NOT NULL DEFAULT FALSE,
    raw_currency VARCHAR(10) NOT NULL DEFAULT 'CNY',
    file_id VARCHAR NOT NULL REFERENCES source_file(file_id),
    row_no INTEGER NOT NULL
);

-- 13. 事实表：银行流水（v0.3 Proof of Cash 核查入口）
CREATE TABLE IF NOT EXISTS bank_entry (
    bank_entry_id VARCHAR PRIMARY KEY,
    entity_id VARCHAR NOT NULL REFERENCES entity(entity_id),
    bank_account VARCHAR NOT NULL,
    booking_date DATE NOT NULL,
    amount DECIMAL(18, 4) NOT NULL,         -- 入账为正，出账为负
    reference VARCHAR,
    counterparty_name VARCHAR,
    description VARCHAR,
    raw_currency VARCHAR(10) NOT NULL DEFAULT 'CNY',
    file_id VARCHAR NOT NULL REFERENCES source_file(file_id),
    row_no INTEGER NOT NULL
);

-- 14. 运行记录与异常裁定表（串联 FDD 发现 -> 交易价格调整桥梁）
CREATE TABLE IF NOT EXISTS check_run (
    run_id VARCHAR PRIMARY KEY,
    entity_id VARCHAR NOT NULL REFERENCES entity(entity_id),
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    matrix_sha256 VARCHAR(64) NOT NULL,
    target_basis VARCHAR NOT NULL,
    target_scope VARCHAR NOT NULL,
    status VARCHAR NOT NULL                 -- 'completed' | 'failed'
);

CREATE TABLE IF NOT EXISTS exception (
    exception_id VARCHAR PRIMARY KEY,
    run_id VARCHAR NOT NULL REFERENCES check_run(run_id),
    check_id VARCHAR NOT NULL,
    entity_id VARCHAR NOT NULL REFERENCES entity(entity_id),
    period_id VARCHAR REFERENCES period(period_id),
    dimension VARCHAR NOT NULL,
    severity VARCHAR NOT NULL CHECK (severity IN ('high', 'medium', 'low')),
    deal_impact_type VARCHAR NOT NULL CHECK (
        deal_impact_type IN ('qoe_ebitda', 'nwc_peg', 'net_debt', 'integrity_blocker', 'info_only')
    ),
    raw_flagged_amount DECIMAL(18, 4),
    summary VARCHAR NOT NULL,
    evidence_json VARCHAR NOT NULL,         -- JSON: [{file_id, row_no, page_no, ref}]
    status VARCHAR NOT NULL DEFAULT 'open' CHECK (
        status IN ('open', 'investigating', 'explained', 'confirmed_adj', 'unresolved')
    ),
    confirmed_adj_amount DECIMAL(18, 4) DEFAULT 0, -- 分析师确认进入对应 Bridge 的有符号调整额（如 -1500000 代表调减 EBITDA）
    recurring_flag BOOLEAN,
    owner VARCHAR,
    note VARCHAR,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
