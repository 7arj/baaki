# Baaki run report (brain: openai)

Ledger: 120 overdue invoices across 40 debtors, ₹82,95,063.00 receivable, 45-day horizon.

| metric | do nothing | naive reminders | Baaki agent |
|---|---:|---:|---:|
| recovered | ₹26,58,457.00 | ₹42,99,235.00 | **₹53,06,880.55** |
| recovery rate | 32.0% | 51.8% | **64.0%** |
| via Razorpay link | – | – | ₹37,53,085.05 |
| via human after escalation | – | – | ₹8,06,538.50 |
| discount written off | – | – | ₹0.00 |
| contacts sent | 0 | 1240 | 301 |
| contacts per ₹1L recovered | – | 28.84 | 5.67 |
| compliance violations | 0 | 919 | 0 |
| out-of-policy proposals blocked | – | – | 1 |
| gateway failures handled | – | – | 2 |
| escalated / stopped for a human | – | – | 40 / 0 |

Incremental recovery vs do-nothing: **₹26,48,423.55**; vs naive: **₹10,07,645.55**.

## Risk model (held-out)
Label: invoice NOT paid within 30 days with zero intervention. Split: by debtor id hash (40% held out); no debtor appears in both. Holdout n=41, base rate 0.488.

| threshold | precision | recall | F1 | TP/FP/FN/TN |
|---|---:|---:|---:|---|
| 0.5 | 0.741 | 1.0 | 0.851 | 20/7/0/14 |
| 0.7 | 0.833 | 1.0 | 0.909 | 20/4/0/17 |

## Exception list (needs a human)

| invoice | debtor | outstanding | status | reason |
|---|---|---:|---|---|
| inv_0024 | Mehta Traders | ₹2,40,866.00 | escalated | Debtor explicitly disputes the invoice (quality rejected / requests credit note). Policy requires escalation to a human when the debtor disputes. Also pause further collection and request QC evidence to enable resolution. |
| inv_0050 | Verma Pharma Distributors | ₹2,40,785.00 | escalated | Reached automated contact cap (6 messages). Invoice inv_0050 is 44 days overdue, outstanding ₹2,40,785.00. Debtor Verma Pharma Distributors (Pune) has 7 prior invoices with 6 prior late payments; avg days late 37.7. Risk score 0.951. No payments received. Recommend human follow-up to negotiate payment (possible hold on future deliveries, request payment schedule, or decide next steps). |
| inv_0065 | Chawla Pharma Distributors | ₹2,40,777.00 | escalated | Customer reported cash-flow hardship and requested to split the invoice. Offering an installment plan is appropriate, but immediate automated plan creation is blocked by the 3-day contact-gap policy; escalate to a human as required by policy for hardship cases. |
| inv_0010 | Chawla Hardware | ₹2,40,403.00 | escalated | Debtor reports cash-flow hardship and requests to split payment. Offer_installment_plan blocked by contact-gap rule (last contact 2 days ago; min gap 3 days). Customer has history of late payments and prior partials; human review recommended to approve a payment plan or hardship handling. |
| inv_0039 | Bose Hardware | ₹2,40,057.00 | escalated | Debtor reports cash-flow hardship and requests to split the invoice. Per policy, hardship cases must be escalated and offering an installment plan is currently blocked because last contact was 2 days ago (minimum 3 days). Escalation needed so a human can approve a plan and authorize a response. |
| inv_0061 | Chawla Hardware | ₹1,20,333.00 | escalated | Debtor replied proposing to pay half now and the rest next month. Creating an installment plan or sending payment-link/ reminder is blocked because last contact was 2 days ago (policy requires 3-day gap). Policy instructs to escalate when the customer requests a plan but system actions are blocked. Escalate so a human can review and approve a split-payment arrangement or respond with terms; customer has a high risk score and history of late payments, so human review is warranted. |
| inv_0052 | Joshi Logistics | ₹87,677.47 | escalated | Contact cap reached after 6 automated outreaches. Invoice inv_0052 (packaging film — PO 6488) partially paid; outstanding ₹87,677.47 (paise 8,767,747), 43 days overdue. Debtor previously offered 40% and paid ~₹87,669.53 on day 8. High risk_score 0.939 and history of late payments. Recommend human review and decide next step (authorise further contact, offer a formal installment plan up to 3 instalments, or negotiate a small early-settlement discount up to policy limit). |
| inv_0102 | Rao Electricals | ₹85,856.00 | escalated | Debtor claims payment was made in July; possible payment reconciliation issue or misapplied payment. |
| inv_0038 | Rao Electricals | ₹85,624.00 | escalated | Debtor explicitly disputes the invoice (claims short-shipment). Policy requires escalation to a human for disputes. Recent contact was 1 day ago so other outbound actions are blocked; escalate ensures operations/sales can verify the claim and decide correction before further collection. |
| inv_0067 | Nair Logistics | ₹85,253.00 | escalated | Reached max automated contacts (6); invoice inv_0067 is 30 days overdue, outstanding ₹85,253.00, high risk score 0.924, debtor Nair Logistics (prior late count 3, avg days late 31.3). No inbound response. Recommend human outreach to negotiate payment or decide next steps. |
| inv_0111 | Pillai Electricals | ₹85,213.00 | escalated | Debtor (Pillai Electricals) reports cash-flow problems and requests to split payment. Policy blocks auto-offer due to contact-gap (last contact 1 day ago; min 3 days). Escalate for manual review and to propose an installment plan (max 3 installments, min first installment 25% or as agreed). |
| inv_0080 | Chawla Hardware | ₹85,064.00 | escalated | Customer promised 40% on day 9 and remainder in 3 weeks but promised_pay_day has passed without payment. Invoice inv_0080 is 50 days overdue, last contact 23 days ago, high risk_score 0.933 and history of late payments. Recommend human intervention to pursue payment and decide next steps. |
| inv_0060 | Verma Pharma Distributors | ₹60,066.00 | escalated | Automated contact cap (6) has been reached and further automated reminders or payment-link offers are blocked. Debtor is high-risk with repeated late payments and invoice is significantly overdue (60 days). Escalation to a human collector is the appropriate next step. |
| inv_0063 | Bose Hardware | ₹57,323.48 | escalated | Automated contact cap (6) has been reached and the invoice remains partially unpaid with a high risk score and prior late history. The customer made a partial payment but has not committed a firm date for the remaining balance. Human intervention is appropriate to negotiate and obtain a firm commitment while preserving the relationship. |
| inv_0017 | Desai Garments | ₹45,770.00 | escalated | Customer requested to pay half now and the rest next month. Automated offer actions are blocked due to minimum contact gap (1 day since last contact; required 3 days). Requires human decision to approve/installment terms. |
| inv_0109 | Joshi Logistics | ₹45,625.00 | escalated | Debtor reports cash-flow difficulty and requests to split payment. Offer_installment_plan is currently blocked by contact-gap policy (last contact 1 day ago, min gap 3 days). High risk score (0.937), prior late behaviour (8/9). Needs human review to approve a hardship installment plan or alternative arrangement. |
| inv_0112 | Verma Pharma Distributors | ₹45,381.00 | escalated | Automated contact cap reached (6). Debtor is high-risk with repeated late history and invoice now 21 days overdue. Policy allows escalation to human — next step is a personal call and tailored resolution (installments or small discount) or dispute handling. |
| inv_0049 | Nair Logistics | ₹45,372.00 | escalated | Reached max automated contacts (6) with no payment; invoice 49 days overdue, high risk score 0.916. Request human intervention to negotiate or decide next steps. |
| inv_0029 | Chawla Pharma Distributors | ₹32,471.00 | escalated | Debtor reports cash-flow hardship and explicitly asked to split the payment. Policy requires escalation for hardship and several customer-contact actions are currently blocked by the minimum gap (2 days since last contact; min 3 days). Escalate to a human agent to review and propose an appropriate installment plan or other concessions. |
| inv_0077 | Mehta Autoparts | ₹32,039.00 | escalated | Reached automated contact cap (6). Invoice inv_0077 is 57 days overdue, outstanding ₹32,039.00, high risk score 0.908, debtor has prior late history. Last inbound: 'who is this?' on day 15. No promised pay date. |
| inv_0069 | Joshi Logistics | ₹30,128.50 | escalated | Debtor replied asking to pay half now and the rest next month (partial_offer). Policy blocks offering an installment plan or creating a payment link because last contact was 1 day ago and minimum gap is 3 days. Given the high risk score (0.938), history of late payments (8 of 9 prior invoices), and the debtor's explicit request for a split payment, escalate to a human collector so they can review and decide an appropriate payment arrangement. Do not send another outbound message now to respect the contact-gap rule. |
| inv_0013 | Chawla Hardware | ₹24,249.00 | escalated | Debtor requests splitting payment due to cash-flow issues but minimum contact gap prevents automated plan. Please review and approve an installment plan or alternative arrangement. |
| inv_0018 | Banerjee Textiles | ₹22,564.00 | escalated | Debtor offered to pay 50% now and remainder next month; policy blocks automated offer today due to contact-gap. Recommend human approval to accept 50% now (₹22,564.00) and balance in 30 days. High risk score and repeated lateness noted; require human review before committing. |
| inv_0091 | Chawla Foods | ₹19,357.00 | escalated | Invoice inv_0091 (₹19,357.00) is 21 days overdue, contact_count 6 reached and automated contact cap hit. Debtor Chawla Foods has high risk score (0.937) and a history of late payments (9/10). No payment received and no inbound response. Please review for human-led outreach (phone call or negotiated plan) and decide next steps. |
| inv_0081 | Chawla Hardware | ₹19,154.00 | escalated | Debtor (Chawla Hardware) reports cash-flow hardship and requested to 'split this'. Customer is 45 days overdue, outstanding ₹19,154.00, contact_count 4, last contact 1 day ago so automated plan/offer actions are blocked by min-gap policy. Risk score high (0.926). Prior late history: 7 of 12. Please advise allowable installment plan or manual outreach. |
| inv_0087 | Pillai Prints | ₹18,633.00 | escalated | Reached automated contact cap (6) without payment. Invoice inv_0087 — Pillai Prints, Surat. Outstanding ₹18,633.00 (1,863,300 paise), 19 days overdue. High risk score 0.939; prior customer history: 8 prior invoices, 6 prior late, avg days late 40.5. No inbound response. Recommend human follow-up: (1) phone call to accounts contact at Pillai Prints to confirm reasons for non-payment; (2) if hardship, consider offering installment plan up to 3 installments with first installment >=25% and intervals <=30 days; (3) if willing to settle immediately, offer small early-settlement discount within policy (max 5%) only if days_overdue >=21 — note currently 19 days so suggest wait 2 days or offer installment instead; (4) document any promise-to-pay date and do not send further automated reminders until that date. Please take over contact and record outcomes. |
| inv_0062 | Joshi Logistics | ₹16,319.50 | escalated | Debtor reports cash-flow hardship and requested to split the invoice. Offering an installment plan is reasonable, but policy blocks offering plans now because last contact was 1 day ago (minimum gap 3 days). Policy also requires escalation for hardship. Escalating to a human ensures a reviewed, allowed proposal and maintains relationship; message asks for the debtor's preferred split to speed resolution. |
| inv_0099 | Bose Hardware | ₹16,298.00 | escalated | Automated contact cap reached (6) and customer previously promised a 40% partial payment but no payment/reference recorded. Policy allows escalation to human; human intervention (phone/email) is the next appropriate and cost-effective step to secure payment or negotiate a formal plan. |
| inv_0015 | Banerjee Textiles | ₹12,454.00 | escalated | Customer explicitly reports cash-flow hardship and requests to split the invoice. Company policy requires escalation on hardship and outbound payment-plan actions are currently blocked due to the required 3-day contact gap. Escalating lets a human review high-risk customer history and approve any payment plan or concessions before responding. |
| inv_0014 | Chawla Hardware | ₹12,326.00 | escalated | Customer reports cash-flow hardship and asks to split the outstanding amount. Customer earlier promised 40% (due day 8) which was not received. Needs human review to propose an appropriate hardship plan or approve split/installments. |
| inv_0019 | Mehta Autoparts | ₹12,277.00 | escalated | Reached maximum automated contacts (6) for a high-risk account with no payment; invoice inv_0019 is 34 days overdue, outstanding ₹12,277.00. Customer has prior late behaviour (7/10) and high risk score 0.913. No inbound replies. Recommend manual follow-up to decide next step (phone call, relationship manager outreach, or negotiated plan). |
| inv_0116 | Chawla Electricals | ₹12,258.00 | escalated | Customer reported cash-flow hardship and requested to split payment. Invoice inv_0116 outstanding ₹12,258.00 (1,225,800 paise), 36 days overdue. Policy blocks automated offer now (minimum gap 3 days since last contact). Recommend human review and approve an installment plan: up to 3 installments, ~15 days between installments, first installment >=25% (proposed 306,450 paise = ₹3,064.50). Customer has history: 9 prior invoices, 7 late, avg days late 37, 2 prior partials. Please decide and respond with formal plan or alternative. |
| inv_0036 | Joshi Logistics | ₹12,253.50 | escalated | Debtor explicitly offered a partial payment (half now, rest next month). Automated creation of installment plan or payment link is blocked because contact cap (6) has been reached, so escalation to a human agent is the least-cost allowed next step. Provide clear recommendation and options to speed resolution while protecting merchant interests. |
| inv_0023 | Verma Pharma Distributors | ₹12,229.00 | escalated | Invoice inv_0023 (₹12,229.00) is 28 days overdue, contact_count=6 (max reached), high risk_score 0.948, customer has 6 prior late payments and avg_days_late 37.7. Previous automated messages included a 3% early-settlement offer. Recommend urgent human outreach: personal phone call to accounts payable contact in Verma Pharma Distributors, confirm reason for delay, request firm promised pay date, consider holding further shipments, and decide whether to offer a structured installment plan or revised settlement (human to approve). Please review for dispute or hardship; escalate further if customer disputes. |
| inv_0106 | Pillai Electricals | ₹12,055.00 | escalated | Debtor (Pillai Electricals) proposes to pay 50% now and the rest next month. System blocks creating payment links or offering plans due to contact-gap rule. Recommend human review and approve a 2-installment plan: 50% now (₹6,027.50 / 602750 paise) via existing link, remaining 50% due in 30 days. Note: invoice inv_0106 outstanding ₹12,055.00 (1205500 paise), 34 days overdue, risk_score 0.931, prior_late_count 3, prior_partial_payments 3. Last outbound contact was 1 day ago; debtor sent inbound request today. |
| inv_0006 | Mehta Autoparts | ₹12,050.00 | escalated | Debtor inbound message 'who is this?' — unclear identity/intent and minimum contact gap prevents automated reply. Request human review. |
| inv_0084 | Bose Hardware | ₹9,384.00 | escalated | Debtor explicitly requested to split the invoice (hardship). Offering an instalment plan is appropriate, but the system blocks creating/offering a plan now due to the minimum gap since last contact (2 days < 3 days). Per policy, escalate to a human so they can review and respond. Message acknowledges the debtor, asks for helpful details, and remains courteous and brief. |
| inv_0035 | Nair Logistics | ₹9,316.00 | escalated | Reached max automated contact cap (6); invoice inv_0035 ₹9,316 overdue 39 days, high risk_score 0.914, debtor previously asked 'who is this?'. Please review and advise next steps (manual outreach, updated contact details, or approval for further offers). |
| inv_0054 | Chawla Foods | ₹9,238.00 | escalated | Invoice inv_0054 (₹9,238.00) is 57 days overdue with 6 prior automated contacts (policy cap reached). Debtor Chawla Foods has a high risk score (0.924), a history of late payments (9 of 10 prior invoices late) and prior partials. All lower-cost actions (reminder, payment link, installment or discount offers) are blocked by contact cap. Escalate to human for review and next steps (possible manual outreach, negotiate settlement, or decide on further escalation). |
| inv_0076 | Mehta Autoparts | ₹8,877.00 | escalated | Invoice inv_0076 is 44 days overdue (₹8,877.00). Debtor Mehta Autoparts acknowledged with 'ok' on day 17; 5-day wait period has elapsed with no payment. Automated contact cap (6) reached. Risk score 0.91 and history of late payments (7 of 10). Recommend human outreach (phone) to confirm payment timeline, consider offering a limited early-settlement discount up to 3% or a short 2–3 installment plan if merchant approves, or agree a firm payment date. Do not threaten; keep tone conciliatory. |

## LLM usage
openai model `gpt-5-mini`, 335 calls, 69 fallbacks to rules, 491500 in / 140882 out tokens (0 cache reads).

Fallback causes:
- day 3 inv_0050: SimulatedOutage: injected LLM outage
- day 3 inv_0010: SimulatedOutage: injected LLM outage
- day 3 inv_0065: SimulatedOutage: injected LLM outage
- day 3 inv_0031: SimulatedOutage: injected LLM outage
- day 3 inv_0043: SimulatedOutage: injected LLM outage
- day 3 inv_0039: SimulatedOutage: injected LLM outage
- day 3 inv_0032: SimulatedOutage: injected LLM outage
- day 3 inv_0026: SimulatedOutage: injected LLM outage
- day 3 inv_0052: SimulatedOutage: injected LLM outage
- day 3 inv_0024: SimulatedOutage: injected LLM outage
- day 3 inv_0061: SimulatedOutage: injected LLM outage
- day 3 inv_0115: SimulatedOutage: injected LLM outage
- day 3 inv_0033: SimulatedOutage: injected LLM outage
- day 3 inv_0111: SimulatedOutage: injected LLM outage
- day 3 inv_0067: SimulatedOutage: injected LLM outage
- day 3 inv_0063: SimulatedOutage: injected LLM outage
- day 3 inv_0108: SimulatedOutage: injected LLM outage
- day 3 inv_0104: SimulatedOutage: injected LLM outage
- day 3 inv_0030: SimulatedOutage: injected LLM outage
- day 3 inv_0102: SimulatedOutage: injected LLM outage

Decision sources: {'openai': 335, 'openai+rules(rogue)': 1, 'openai->rules': 69}
