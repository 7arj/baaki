# Baaki run report (brain: rules)

Ledger: 120 overdue invoices across 40 debtors, ₹82,95,063.00 receivable, 45-day horizon.

| metric | do nothing | naive reminders | Baaki agent |
|---|---:|---:|---:|
| recovered | ₹26,58,457.00 | ₹42,99,235.00 | **₹61,72,458.78** |
| recovery rate | 32.0% | 51.8% | **74.4%** |
| via Razorpay link | – | – | ₹53,20,673.78 |
| via human after escalation | – | – | ₹3,32,300.15 |
| discount written off | – | – | ₹21,577.85 |
| contacts sent | 0 | 1265 | 294 |
| contacts per ₹1L recovered | – | 29.42 | 4.76 |
| compliance violations | 0 | 934 | 0 |
| out-of-policy proposals blocked | – | – | 1 |
| gateway failures handled | – | – | 2 |
| escalated / stopped for a human | – | – | 9 / 25 |

Incremental recovery vs do-nothing: **₹35,14,001.78**; vs naive: **₹18,73,223.78**.

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
| inv_0050 | Verma Pharma Distributors | ₹2,40,785.00 | stopped | no resolution after 4 automated contacts; recommend a human call or write-off review |
| inv_0010 | Chawla Hardware | ₹2,40,403.00 | stopped | no resolution after 4 automated contacts; recommend a human call or write-off review |
| inv_0026 | Bose Electricals | ₹2,40,257.00 | escalated | dispute raised: 'This invoice is wrong — 20 cartons were short-shipped. Not paying till corrected.' |
| inv_0039 | Bose Hardware | ₹1,21,990.51 | stopped | no resolution after 4 automated contacts; recommend a human call or write-off review |
| inv_0030 | Mehta Ceramics | ₹1,20,240.00 | escalated | dispute raised: 'Quality was rejected by our QC, invoice should be credit-noted.' |
| inv_0102 | Rao Electricals | ₹85,856.00 | escalated | dispute raised: 'Quality was rejected by our QC, invoice should be credit-noted.' |
| inv_0038 | Rao Electricals | ₹85,624.00 | escalated | dispute raised: 'We already paid this in July, check your books.' |
| inv_0067 | Nair Logistics | ₹85,253.00 | stopped | no resolution after 4 automated contacts; recommend a human call or write-off review |
| inv_0111 | Pillai Electricals | ₹85,213.00 | stopped | no resolution after 4 automated contacts; recommend a human call or write-off review |
| inv_0069 | Joshi Logistics | ₹60,257.00 | stopped | no resolution after 4 automated contacts; recommend a human call or write-off review |
| inv_0060 | Verma Pharma Distributors | ₹60,066.00 | stopped | no resolution after 4 automated contacts; recommend a human call or write-off review |
| inv_0063 | Bose Hardware | ₹55,173.91 | stopped | no resolution after 4 automated contacts; recommend a human call or write-off review |
| inv_0080 | Chawla Hardware | ₹54,965.06 | stopped | no resolution after 4 automated contacts; recommend a human call or write-off review |
| inv_0112 | Verma Pharma Distributors | ₹45,381.00 | stopped | no resolution after 4 automated contacts; recommend a human call or write-off review |
| inv_0049 | Nair Logistics | ₹45,372.00 | stopped | no resolution after 4 automated contacts; recommend a human call or write-off review |
| inv_0077 | Mehta Autoparts | ₹32,039.00 | stopped | no resolution after 4 automated contacts; recommend a human call or write-off review |
| inv_0017 | Desai Garments | ₹23,486.87 | escalated | financial hardship reported: 'We are winding up the firm.' |
| inv_0091 | Chawla Foods | ₹19,357.00 | stopped | no resolution after 4 automated contacts; recommend a human call or write-off review |
| inv_0015 | Banerjee Textiles | ₹18,681.00 | stopped | no resolution after 4 automated contacts; recommend a human call or write-off review |
| inv_0087 | Pillai Prints | ₹18,633.00 | stopped | no resolution after 4 automated contacts; recommend a human call or write-off review |
| inv_0036 | Joshi Logistics | ₹18,380.25 | stopped | no resolution after 4 automated contacts; recommend a human call or write-off review |
| inv_0013 | Chawla Hardware | ₹18,186.75 | stopped | no resolution after 4 automated contacts; recommend a human call or write-off review |
| inv_0019 | Mehta Autoparts | ₹12,277.00 | stopped | no resolution after 4 automated contacts; recommend a human call or write-off review |
| inv_0023 | Verma Pharma Distributors | ₹12,229.00 | stopped | no resolution after 4 automated contacts; recommend a human call or write-off review |
| inv_0103 | Mehta Traders | ₹12,131.00 | escalated | dispute raised: 'Quality was rejected by our QC, invoice should be credit-noted.' |
| inv_0006 | Mehta Autoparts | ₹12,050.00 | stopped | no resolution after 4 automated contacts; recommend a human call or write-off review |
| inv_0035 | Nair Logistics | ₹9,316.00 | stopped | no resolution after 4 automated contacts; recommend a human call or write-off review |
| inv_0054 | Chawla Foods | ₹9,238.00 | stopped | no resolution after 4 automated contacts; recommend a human call or write-off review |
| inv_0106 | Pillai Electricals | ₹9,041.25 | stopped | no resolution after 4 automated contacts; recommend a human call or write-off review |
| inv_0076 | Mehta Autoparts | ₹8,877.00 | stopped | no resolution after 4 automated contacts; recommend a human call or write-off review |
| inv_0099 | Bose Hardware | ₹8,407.15 | escalated | financial hardship reported: 'Business is shut since June, we have no funds right now.' |
| inv_0116 | Chawla Electricals | ₹6,983.82 | stopped | no resolution after 4 automated contacts; recommend a human call or write-off review |
| inv_0084 | Bose Hardware | ₹5,587.65 | escalated | financial hardship reported: 'Factory flooded, operations stopped. Cannot pay currently.' |

Decision sources: {'rules': 413, 'rules(rogue)': 1}
