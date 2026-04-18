# Architect Rules

## Design Principles
1. Simple > Clever. If a junior developer can't understand the design, simplify it.
2. Composable > Monolithic. Build small services that can be combined.
3. Configurable > Hardcoded. Everything that might change should be in config.
4. Shared > Duplicated. If two companies need the same thing, build it once in /opt/tickles/shared/.

## Schema Design Checklist
Before presenting any database schema, verify:
- [ ] Every table has a primary key (bigint unsigned auto_increment)
- [ ] Every table has created_at (datetime(3) DEFAULT CURRENT_TIMESTAMP(3))
- [ ] Every table has updated_at where records are modified
- [ ] Columns that are queried frequently have indexes
- [ ] Composite unique keys exist where duplicates must be prevented
- [ ] Foreign key relationships are documented even if not enforced
- [ ] Column names are snake_case with no abbreviations (strategy_id not strat_id)
- [ ] Boolean columns use is_ prefix (is_active, is_paper, is_closed)
- [ ] Enum values are lowercase strings
- [ ] No nullable columns unless NULL has a genuine business meaning

## Interface Design Checklist
Before presenting any service interface, verify:
- [ ] Every function has defined input types and return types
- [ ] Every function documents what exceptions it can raise
- [ ] Error responses are structured and consistent
- [ ] The interface is exchange-agnostic (works for crypto AND CFDs)
- [ ] The interface handles both real and paper/demo trading

## Trading-Specific Architecture Rules
1. Every trade must be traceable: trade → strategy → parameters → backtest
2. Every data point must have a source and timestamp
3. Every price must include the exchange it came from
4. Backtesting and live trading MUST use the exact same signal logic — no separate implementations
5. Position sizing calculations must never exceed available balance

## Output Format
1. Schemas: present as SQL CREATE TABLE statements with comments
2. Interfaces: present as Python abstract classes or protocol definitions
3. Diagrams: describe data flow in text, use mermaid syntax if needed
4. Always end with an "Implementation Order" section listing what to build first
