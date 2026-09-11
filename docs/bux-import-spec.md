# BUX Transaction-History CSV — Import Specification

*Derived from a real export (465 rows, Feb 2022 → May 2026). The personal file is kept only under `data/raw/` (git-ignored). Tests use synthetic rows that follow this spec.*

## File format
- Comma-separated, `.` decimal, no thousands separator, ASCII/UTF-8, header on line 1, one row per booking. Timestamps `YYYY-MM-DD HH:MM:SS.ffffff` in **CET**; the transaction date is the CET calendar date.
- 21 columns (exact header names, in order):
  `Transaction Time (CET), Transaction Category, Transaction Type, Transfer Type, Transaction Amount, Transaction Currency, Cash Balance Amount, Asset Id, Asset Name, Asset Quantity, Asset Price, Asset Currency, Currency Pair, Exchange Rate, Profit And Loss Amount, Profit And Loss Currency, Dividend Currency, Dividend Gross Amount, Dividend Net Amount, Dividend Tax Amount, Transaction Description`
- The open-source converter's camelCase header (`assetId`, …) does **not** match this export. The importer maps by normalised header name (lower-case, non-alphanumerics stripped) and warns on unknown or missing columns.

## Row semantics
| Category | Type | Transfer Type | Meaning | Mapped to |
|---|---|---|---|---|
| trades | Buy Trade (also `Buy  Trade`, double space) | ASSET_TRADE_BUY | **Asset leg**: `Transaction Amount` = gross value in asset currency (+), `Asset Quantity`, `Asset Price` exact (8 dp). No cash balance. | BUY (quantity, price, gross) |
| trades | Buy Trade | CASH_DEBIT | **Cash leg**: `Transaction Amount` = EUR debited (−), `Asset Price` rounded, `Currency Pair`/`Exchange Rate` (see FX), running `Cash Balance Amount`. | BUY (EUR cash, FX) |
| trades | Sell Trade | ASSET_TRADE_SELL / CASH_CREDIT | Same pair, signs reversed. `Profit And Loss Amount` (EUR) = BUX's own realized P/L for the fill (gross of fees). | SELL |
| fees | Trading Fee | CASH_DEBIT | Separate row, `Asset Id` set, description `Order Id: <uuid>` matching the trade's `Order Partial Id: <uuid> - n`. | FEE attributed to the ISIN, linked to the order |
| fees | Subscription Fee | CASH_DEBIT | Monthly plan fee, description `Period: YYYY-MM`. | FEE (account level) |
| tax | Tob | CASH_DEBIT | Belgian *taks op beursverrichtingen*, `Asset Id` set, no order link. | TAX attributed to ISIN |
| tax | Financial Transaction Tax / … Refund | CASH_DEBIT / CASH_CREDIT | French/Italian FTT and refunds, `Asset Id` set. | TAX (refund = positive amount) |
| dividends | Cash Dividend | CASH_CREDIT | `Transaction Amount` = **net EUR actually credited**; `Dividend Gross/Net/Tax Amount` in `Dividend Currency`; `Exchange Rate` as below. | DIVIDEND |
| dividends | Manufactured Cash Dividend | CASH_CREDIT | Dividend substitute while shares were lent out; same columns. | DIVIDEND (note "manufactured") |
| dividends | Cash Dividend Reversal | CASH_DEBIT | Negative amounts, same columns. | DIVIDEND with negative gross/tax |
| interest | Interest Payment | CASH_CREDIT | Cash interest, `Period:` description. | INTEREST |
| others | Security Lending Revenue, Promotional | CASH_CREDIT | Other income, no position. | INCOME |
| deposits | Sepa Deposit, Payment Deposit | CASH_CREDIT | Owner contribution. | DEPOSIT |
| withdrawals (not present in sample) | any | CASH_DEBIT | Owner withdrawal. | WITHDRAWAL (+ warning: first time seen) |
| corporate_actions | General Corporate Action | ASSET_REDEEM | Shares removed (tender offer, reverse-split cash-in-lieu, crypto migration). `Transaction Amount` = −value at `Asset Price`; `Profit And Loss Amount` = BUX's realized P/L. | see pairing |
| corporate_actions | General Corporate Action | CASH_CREDIT | Cash proceeds of a preceding ASSET_REDEEM; description `… Cash` / `… Cash Proceeds`. **May carry a wrong `Asset Id`/`Asset Name`** (observed: Generation Bio proceeds booked under `DE000TUAG109 "Revenue from sales (98)"`). | see pairing |
| corporate_actions | General Corporate Action | ASSET_DEPOSIT | Shares added without cash (observed: "Fixing All Time Metric" re-booking of migrated crypto). | TRANSFER_IN |
| others | Portfolio Transfer | ASSET_REDEEM | Shares moved out without cash. | TRANSFER_OUT |

### Pairing rules
1. **Trade legs**: group by `Order Partial Id` uuid from the description; fallback: same `Asset Id`, same `Asset Quantity`, timestamps within 2 s. Quantity and exact price come from the asset leg; EUR cash from the cash leg. A leg without a partner → imported with a warning (asset leg: FX from ECB rate later; cash leg: quantity/price from its own rounded columns).
2. **Trading Fee → order**: `Order Id` uuid equals the trade's `Order Partial Id` uuid. Fee stays a separate FEE transaction (matches BUX, whose average price excludes fees) but is attributed to the ISIN.
3. **Corporate-action cash**: a corporate_actions CASH_CREDIT is matched to the most recent unmatched corporate_actions ASSET_REDEEM within 30 days whose `Asset Name` appears in the cash row's description or whose `Asset Id` matches. The pair becomes a **SELL** of the redeemed quantity at `cash EUR / quantity` (EUR, fx 1), note = description, `bux_pl` = the redeem row's P/L. Unmatched ASSET_REDEEM → TRANSFER_OUT (position leaves at cost, no cash); unmatched CASH_CREDIT → INCOME with a warning naming the row.

### FX convention
`Currency Pair` = `EUR<CCY>`, `Exchange Rate` = units of CCY per 1 EUR (EURUSD 1.12637 → 28.16 USD = 25.00 EUR). Our `Txn.fx_rate` is EUR per 1 CCY.
- Trades: `fx_rate = |cash-leg EUR| / |asset-leg gross|` (the effective rate; reproduces BUX cash to the cent). EUR assets → exactly 1.
- Dividends: `fx_rate = Transaction Amount (net EUR) / Dividend Net Amount` when net ≠ 0, else `1 / Exchange Rate`. Cent rounding on tiny dividends (net USD 0.02 → EUR 0.01) is absorbed into the effective rate so cash reconciles exactly.

### Identifiers
- `Asset Id` is an ISIN for securities; crypto uses symbols (`LUNA`, `MATIC`, asset type `crypto`, no market data, both positions were transferred out).
- One issuer can appear under two ISINs over time (TUI `DE000TUAG505` → `DE000TUAG109`); they are separate securities unless a corporate action links them.
- Trading currency of a security = `Asset Currency` on its trade rows (not on dividend rows, where it may be the dividend currency).

### Sign conventions (source)
ASSET_TRADE_BUY, ASSET_DEPOSIT, CASH_CREDIT positive; ASSET_TRADE_SELL, ASSET_REDEEM, CASH_DEBIT negative.

### Idempotency
Transaction id = SHA-1 of the raw row text (all 21 fields). Re-importing the same or an overlapping export adds only unseen rows. The raw row is stored as JSON on every transaction.

### Built-in reconciliation checks
- **Cash**: replay all cash rows in file order and compare to `Cash Balance Amount` after each row (must match to the cent; the asset legs have no balance and are skipped).
- **Realized P/L**: per sell/redeem, compare our realized P/L (gross of fees) with `Profit And Loss Amount`.
- **Quantities**: compare final per-ISIN quantity with the BUX app (user-supplied).

### Edge cases seen
Double-space type name; asset leg/cash leg microsecond ordering varies; dividend of USD 0.03 gross / 0.01 tax → EUR 0.01; corporate-action cash row with wrong asset id; crypto migrated out then re-booked and transferred out again (net: out); reverse split leaving cash-in-lieu; tender offer.
