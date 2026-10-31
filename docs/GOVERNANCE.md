# Safety and governance

An agent that can change an ERP needs more than a good prompt. In AERA the language model can
read and propose; every rule that decides whether something happens is deterministic code
with a unit test named after the rule (`test_br_05_...`, `test_v_06_...`). Nothing below lives
only in a prompt.

## Autonomy tiers (BR-05)

| Tier | When | What happens |
|---|---|---|
| **Tier 1 — Auto** | Total cost ≤ USD 25,000 **and** every action reversible **and** plan confidence ≥ 0.85 **and** every check passes **and** the kill switch is off | Executes at once; a sample is audited afterwards |
| **Tier 2 — Approval** | Every other verified plan (cost above USD 25,000 or any irreversible action) | A named approver with a sufficient approval limit decides, before a deadline |
| **Tier 3 — Escalate** | Confidence below 0.85, any blocking check fails, an unconfirmed critical field is needed, or a run limit is hit | A planner investigates; nothing executes |

**Urgent partial execution (BR-22).** When a Tier 2 plan cannot wait — stock-out comes before
the approval deadline plus the lead time — routing splits it: the actions that qualify for
Tier 1 on their own run immediately, the rest wait for approval. Each part has its own plan-part
id and idempotency keys.

**Approval deadline (BR-23).** Deadline = min(stock-out time minus lead time, routed time + 4 h).
A reminder fires at half the time left; at the deadline the request moves to a backup
approver with a sufficient limit, or the case escalates. A late approval is refused and the
plan is re-verified.

**Kill switch.** Admins can put the workspace in advise-only mode: investigation and
recommendations continue, no new execution starts.

## Plan confidence (BR-18)

The model never states its own confidence. It is computed:

```text
confidence = 0.5 × S + 0.3 × C + 0.2 × G
```

- **S** — share of plan figures whose source is SAP, the rate card or a planner confirmation
  and whose value the verifier re-read unchanged.
- **C** — corroboration: 1.0 for an SAP MRP message or two independent verified signals, 0.7
  for one verified external signal.
- **G** — Bedrock Guardrails contextual grounding score of the plan rationale.

## Verifier checks

| ID | Check | Blocking |
|---|---|---|
| V-01 | Every figure has a `sourceRef`; the re-read value equals the plan value | Yes |
| V-02 | Quantities positive and whole in the base unit; shortfall flagged as partial cover; over-cover above 25 % flagged | Yes |
| V-03 | Arrival dates in the future and not after the need date | Yes |
| V-04 | Action types in the allowed set; parameters complete | Yes |
| V-05 | Supplier and plant exist in master data | Yes |
| V-06 | Alternate supplier's compliance status is APPROVED (BR-06) | Yes |
| V-07 | Donor plant keeps its minimum cover; no donor customer order broken (BR-07) | Yes |
| V-08 | Ledger has enough unreserved quantity (BR-08, dry run) | Yes |
| V-09 | Plan cost below the revenue at risk it protects (BR-12) | Yes |
| V-10 | Lead time feasible against the calendar | Yes |
| V-11 | Every recipient in the allowlist (BR-03) | Yes |
| V-12 | No unconfirmed critical field used (BR-02) | Yes |
| V-13 | Contextual grounding score of the rationale ≥ 0.7 | No — feeds G in BR-18 |
| V-14 | Outbound supplier message obeys BR-19 | Yes |
| V-15 | Plan consistent with the latest portfolio allocation | Yes |

On top of these, an **Amazon Bedrock Automated Reasoning** policy encodes the approval,
donor-cover and economic rules as formal logic and checks each plan against them, as an
independent second opinion on the coded checks.

## Business rules

| ID | Rule |
|---|---|
| BR-01 | **SAP is the source of truth.** Quantities, dates, prices and stock come from SAP; action costs from the administered rate card. Signals only trigger and corroborate. |
| BR-02 | **Critical-field confidence.** An extracted field is usable only if Textract confidence ≥ 0.95, it matches SAP exactly, or a planner confirms it. |
| BR-03 | **Recipient allowlist.** Outbound messages only to addresses and numbers held in SAP master data for parties on the case. |
| BR-04 | **Sender verification.** A signal is accepted only if its sender domain or phone number matches a supplier or carrier in SAP master data. |
| BR-05 | **Autonomy tiers** — see above. |
| BR-06 | **Compliance gate.** An alternate supplier is viable only with compliance status APPROVED. |
| BR-07 | **Donor plant protection.** A transfer may not push the donor plant below its minimum cover (default 2 days) or break its confirmed orders. |
| BR-08 | **Reservation.** Stock allocated by a plan is reserved in the ledger by conditional write; the first committed reservation wins. |
| BR-09 | **Idempotency key.** `sha256(caseId \| planVersion \| actionIndex \| actionType \| targetKey)`; a known key returns the stored result without calling SAP. |
| BR-10 | **Undo first.** The undo plan is persisted before the first write. |
| BR-11 | **Run limits.** 20 iterations, 150,000 tokens, 300 s per run; 3 re-plans per case. A limit escalates, never fails silently. |
| BR-12 | **Economic sanity.** Plan cost must be lower than the revenue at risk it protects. |
| BR-13 | **Priority score.** `revenue at risk × urgency` (3 if stock-out < 24 h, 2 if < 72 h, else 1). |
| BR-14 | **Reopen on miss.** No goods receipt by expected arrival + grace (2 h, shortened to reopen at least 1 h before stock-out) reopens the case. |
| BR-15 | **De-duplication.** Signals about the same PO and material within 72 h belong to one case. |
| BR-16 | **Chat cannot authorise.** No planner or chat instruction can change tier, threshold or allowlist. |
| BR-17 | **Reversibility classes.** Reversible: STO before goods issue, PO date change, schedule-line split. Irreversible: air freight, new PO to an alternate supplier, anything with a cancellation fee. |
| BR-18 | **Plan confidence** — see above. |
| BR-19 | **Supplier messaging limits.** Questions only about the supplier's own open POs on the case; never prices, quantities, payment or contract terms. |
| BR-20 | **Portfolio allocation.** Competing cases are allocated by the optimiser, not by the agent or by arrival order. |
| BR-21 | **External agents are untrusted.** A2A / MCP requests are signals: they cannot approve, execute or configure, and are rate-limited. |
| BR-22 | **Urgent partial execution** — see above. |
| BR-23 | **Approval deadline** — see above. |

## Prompt-injection and untrusted content

Supplier email, PDFs, photos and external agents are attacker-controlled. AERA treats them as
evidence and layers its defences:

1. **Sender check** against SAP master data (BR-04); look-alike domains are quarantined.
2. **Hidden-text extraction** (white text, tiny fonts, PDF layers) before scanning.
3. **Bedrock Guardrails** `ApplyGuardrail` with prompt-attack detection at HIGH; content is
   wrapped in input tags so the model sees it as data.
4. **Read-only tools.** Even a successful injection can only make the agent read or propose.
5. **Deterministic verifier and routing.** A proposed plan still has to pass V-01..V-15 and the
   tier rules; the model cannot lower a threshold or skip approval (BR-16).
6. **Audit.** Every quarantined signal is recorded with its reason.

In the reference run, six hostile signals (injected instructions, look-alike senders) were
quarantined at the gate, and a PDF carrying an embedded instruction was blocked by the gate
defence.

## Execution guarantees

| Guarantee | How |
|---|---|
| No double booking | Ledger reservation by conditional write (BR-08); the optimiser shares scarce stock |
| No duplicate SAP documents | Idempotency key per action (BR-09); replaying an execution returns the stored result |
| Always reversible or approved | Undo plan saved before the first write (BR-10); irreversible actions require Tier 2 |
| No stale decisions | Plan version hash, stock and kill switch revalidated before writing; stale approvals refused |
| Failures do not leave half a plan | Compensation runs the saved undo steps |
| Goods that never arrive are noticed | Goods-receipt check; the case reopens itself (BR-14) |

## Security

- **Least privilege per zone.** Only the execution workflow can write to SAP; only the notifier
  can send messages; agent tools are read-only and each has its own role.
- **Secrets** live in AWS Secrets Manager, are read at runtime and are never logged. The
  repositories contain no credentials; a secret scan and a dependency vulnerability audit run
  on every commit.
- **Data residency.** One region (`us-east-1`); model access limited to direct regional
  inference or the US geographic inference profile, enforced by deploy checks and IAM.
- **Encryption.** Every table and bucket uses the project KMS key; HTTPS with TLS 1.2+ only.
- **Audit trail.** Decisions, approvals, overrides and configuration changes go to an audit
  table archived to an S3 Object Lock bucket in compliance mode.
- **Cost control.** An AWS Budget with alerts must exist before any other resource is
  deployed; every deploy command verifies it first.
