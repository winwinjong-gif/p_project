-- Financial collection tables from design v2.0. Apply to the project's PostgreSQL database.
CREATE TABLE IF NOT EXISTS mr_stocks (
    code VARCHAR(12) PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    corp_code CHAR(8),
    listed_at DATE,
    delisted_at DATE,
    universe_as_of DATE NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')
);

CREATE TABLE IF NOT EXISTS mr_daily_prices (
    code VARCHAR(12) NOT NULL REFERENCES mr_stocks(code),
    date DATE NOT NULL,
    open DECIMAL(20,6) NOT NULL,
    high DECIMAL(20,6) NOT NULL,
    low DECIMAL(20,6) NOT NULL,
    close DECIMAL(20,6) NOT NULL,
    volume BIGINT NOT NULL CHECK (volume >= 0),
    source VARCHAR(30) NOT NULL,
    price_basis VARCHAR(40) NOT NULL,
    collected_at TIMESTAMP NOT NULL,
    PRIMARY KEY (code, date)
);

CREATE TABLE IF NOT EXISTS mr_financial_reports (
    id VARCHAR(36) PRIMARY KEY,
    code VARCHAR(12) NOT NULL REFERENCES mr_stocks(code),
    receipt_no CHAR(14) NOT NULL,
    business_year SMALLINT NOT NULL,
    scope VARCHAR(3) NOT NULL CHECK (scope IN ('CFS','OFS')),
    published_at TIMESTAMP NOT NULL,
    available_date DATE NOT NULL,
    supersedes_id VARCHAR(36) REFERENCES mr_financial_reports(id),
    source_file TEXT NOT NULL,
    source_sha256 CHAR(64) NOT NULL,
    UNIQUE (receipt_no, scope)
);

CREATE TABLE IF NOT EXISTS mr_financial_items (
    id VARCHAR(36) PRIMARY KEY,
    report_id VARCHAR(36) NOT NULL REFERENCES mr_financial_reports(id),
    statement_type VARCHAR(10) NOT NULL,
    account_id TEXT NOT NULL,
    account_name TEXT NOT NULL,
    account_detail TEXT NOT NULL,
    period_name VARCHAR(50) NOT NULL,
    amount DECIMAL(30,6),
    currency VARCHAR(10) NOT NULL,
    unit_scale INTEGER NOT NULL CHECK (unit_scale > 0)
);
