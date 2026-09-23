# AERA — backend, agent and infrastructure

AERA (Autonomous Exception Resolution Agent) takes a supply chain exception from a raw external signal — a supplier email, a PDF order confirmation, a photographed confirmation sent by WhatsApp, or a carrier notice — through to a verified, approved and reversible correction in SAP S/4HANA.

This repository holds the backend: the reasoning agent, the deterministic control plane, the infrastructure, the SAP Mirror service and the evaluation harness. The web console lives in `aera-fe`.

## What it does

- **Reads what planners read:** supplier email and PDFs, WhatsApp photos, carrier events, SAP MRP exceptions — in English, Bahasa Indonesia and German.
- **Grounds every number in SAP.** Figures are fetched, never invented. Where a message and SAP disagree, SAP wins.
- **Quantifies the impact:** which customer orders are exposed, when stock runs out, how much revenue is at risk.
- **Proposes options** (inter-plant transfer, expedite, PO re-schedule, alternate supplier) with cost, arrival date and a time-phased stock projection, and optimises across competing cases.
- **Decides how much to automate:** small, reversible, high-confidence actions run automatically with sampled audit; large or irreversible ones need a named approver.
- **Executes safely:** deterministic workflow, stock reservation, undo plan saved before the first write, idempotent writes, and the case reopens itself if the goods never arrive.
- **Asks the supplier directly** when a fact is missing, within strict messaging rules.

## Architecture

Reasoning runs on Amazon Bedrock AgentCore with the Strands Agents SDK and has read-only tools. Verification, routing, stock reservation and all writes are deterministic services: AWS Step Functions, Lambda, DynamoDB, EventBridge. Document and language understanding use Amazon Textract and Comprehend; safety uses Amazon Bedrock Guardrails. SAP S/4HANA Cloud APIs are the system of record, with an SAP CAP service on SAP BTP for writes.

## Layout

```text
infra/         AWS CDK stacks
services/      shared, rules, ingestion, agent, tools, verifier, routing, optimizer,
               workflow, dialogue, analytics, lab, interop, monitor, api
sap-mirror/    SAP CAP service with the S/4HANA schema
data/synthetic/ generators for test signals
eval/          evaluation harness and cases
```

## Getting started

```
make bootstrap
make mirror-local
make test
```

Built for the AWS / SAP Agentic AI Hackathon, track: Intelligent Supply Chain.
