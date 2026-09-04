# Baaki run report (brain: openai)

Ledger: 120 overdue invoices across 40 debtors, ₹82,95,063.00 receivable, 10-day horizon.

| metric | do nothing | naive reminders | Baaki agent |
|---|---:|---:|---:|
| recovered | ₹21,11,351.00 | ₹42,66,330.00 | **₹56,23,874.07** |
| recovery rate | 25.5% | 51.4% | **67.8%** |
| via Razorpay link | – | – | ₹48,45,501.22 |
| via human after escalation | – | – | ₹2,58,888.00 |
| discount written off | – | – | ₹21,577.85 |
| contacts sent | 0 | 412 | 294 |
| contacts per ₹1L recovered | – | 9.66 | 5.23 |
| compliance violations | 0 | 127 | 0 |
| out-of-policy proposals blocked | – | – | 0 |
| gateway failures handled | – | – | 0 |
| escalated / stopped for a human | – | – | 11 / 8 |

Incremental recovery vs do-nothing: **₹35,12,523.07**; vs naive: **₹13,57,544.07**.

## Risk model (held-out)
Label: invoice NOT paid within 30 days with zero intervention. Split: by debtor id hash (40% held out); no debtor appears in both. Holdout n=41, base rate 0.488.

| threshold | precision | recall | F1 | TP/FP/FN/TN |
|---|---:|---:|---:|---|
| 0.5 | 0.741 | 1.0 | 0.851 | 20/7/0/14 |
| 0.7 | 0.833 | 1.0 | 0.909 | 20/4/0/17 |

## Exception list (needs a human)

| invoice | debtor | outstanding | status | reason |
|---|---|---:|---|---|
| inv_0024 | Mehta Traders | ₹2,40,866.00 | escalated | dispute raised: 'This invoice is wrong — 20 cartons were short-shipped. Not paying till corrected.' |
| inv_0026 | Bose Electricals | ₹2,40,257.00 | escalated | dispute raised: 'This invoice is wrong — 20 cartons were short-shipped. Not paying till corrected.' |
| inv_0030 | Mehta Ceramics | ₹1,20,240.00 | escalated | dispute raised: 'Quality was rejected by our QC, invoice should be credit-noted.' |
| inv_0102 | Rao Electricals | ₹85,856.00 | escalated | dispute raised: 'Quality was rejected by our QC, invoice should be credit-noted.' |
| inv_0038 | Rao Electricals | ₹85,624.00 | escalated | dispute raised: 'We already paid this in July, check your books.' |
| inv_0111 | Pillai Electricals | ₹85,213.00 | stopped | no resolution after 4 automated contacts; recommend a human call or write-off review |
| inv_0060 | Verma Pharma Distributors | ₹60,066.00 | stopped | no resolution after 4 automated contacts; recommend a human call or write-off review |
| inv_0110 | Rao Electricals | ₹32,926.00 | escalated | dispute raised: 'This invoice is wrong — 20 cartons were short-shipped. Not paying till corrected.' |
| inv_0079 | Rao Electricals | ₹32,079.00 | escalated | dispute raised: 'Quality was rejected by our QC, invoice should be credit-noted.' |
| inv_0017 | Desai Garments | ₹23,486.87 | escalated | financial hardship reported: 'We are winding up the firm.' |
| inv_0109 | Joshi Logistics | ₹19,577.21 | stopped | no resolution after 4 automated contacts; recommend a human call or write-off review |
| inv_0036 | Joshi Logistics | ₹18,380.25 | stopped | no resolution after 4 automated contacts; recommend a human call or write-off review |
| inv_0013 | Chawla Hardware | ₹18,186.75 | stopped | no resolution after 4 automated contacts; recommend a human call or write-off review |
| inv_0099 | Bose Hardware | ₹16,814.30 | escalated | financial hardship reported: 'Business is shut since June, we have no funds right now.' |
| inv_0081 | Chawla Hardware | ₹14,365.50 | stopped | no resolution after 4 automated contacts; recommend a human call or write-off review |
| inv_0103 | Mehta Traders | ₹12,131.00 | escalated | dispute raised: 'Quality was rejected by our QC, invoice should be credit-noted.' |
| inv_0011 | Joshi Logistics | ₹10,263.31 | stopped | no resolution after 4 automated contacts; recommend a human call or write-off review |
| inv_0035 | Nair Logistics | ₹9,316.00 | stopped | no resolution after 4 automated contacts; recommend a human call or write-off review |
| inv_0084 | Bose Hardware | ₹5,587.65 | escalated | financial hardship reported: 'Factory flooded, operations stopped. Cannot pay currently.' |

Decision sources: {'rules': 385}
