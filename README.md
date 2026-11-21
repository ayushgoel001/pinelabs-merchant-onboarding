# Autonomous Merchant Onboarding Platform

**Pine Labs Case Study 01 | End-to-End Merchant Onboarding**

An end-to-end merchant onboarding platform that automates document validation, verification workflows, exception handling, and Merchant ID issuance.

The system is designed to handle real-world onboarding challenges such as missing documents, inconsistent information, temporary provider failures, duplicate applications, and cases requiring human review.

It combines **OCR-based document processing, asynchronous verification, durable workflow orchestration, and human-in-the-loop decisions** in a single auditable application.

## System Architecture

![Merchant Onboarding Platform Architecture](PineLabs-Case01-Starter/evidence/architecture/architecture.png)

### Technology Stack

| Component | Technology |
|---|---|
| Backend | Python 3.11, FastAPI |
| Frontend | Jinja2, HTML, CSS, JavaScript |
| Database | SQLite (WAL mode) |
| Document Processing | Tesseract OCR, PyMuPDF, Pillow |
| Background Processing | Separate Python worker process |
| Verification | Simulated PAN, GST, BANK, RISK services |
| Communication | HTTP requests and signed asynchronous callbacks |
| Testing | pytest |

### How the system works

1. **Application submission:** Merchants submit their business information and supporting documents.
2. **Document validation:** OCR extracts information from uploaded documents and checks consistency with submitted details.
3. **Workflow orchestration:** A persistent state machine tracks evidence, pending work, verification results, and exceptions.
4. **Asynchronous verification:** A background worker dispatches PAN, GST, BANK, and RISK verification requests.
5. **Exception handling:** Missing documents, verification failures, duplicate applications, and risk referrals trigger appropriate holds or operator reviews.
6. **Approval and issuance:** Once every required condition qualifies, the approval gate records the decision and initiates simulated Merchant ID issuance.

SQLite persists application state, verification attempts, callback receipts, operator decisions, and audit records, allowing interrupted workflows to resume.

## Key Features

### Merchant Onboarding

- Structured business and bank information collection.
- Identity, GST, and bank document uploads in PDF, PNG, and JPEG formats.
- OCR-based field extraction and confidence checks.
- Application status tracking and verification progress.
- Document correction and resubmission without restarting the application.
- Merchant ID generation after successful approval.

### Automated Verification

- Four independently executed verification checks: PAN, GST, BANK, and RISK.
- Asynchronous requests and signed callbacks.
- Automatic retries for transient provider failures.
- Idempotent callback processing to handle duplicate responses.
- Persistent verification attempts and audit records.
- Deadline handling and recovery of failed background jobs.

### Operator Desk

- Protected operator access through a single-use authentication link.
- Review queue for applications requiring intervention.
- Detailed application evidence and verification history.
- Reasoned decisions for corrections, risk referrals, and rejections.
- Duplicate-merchant detection and approval safeguards.
- Operational metrics for onboarding outcomes, provider response times, and retries.

### Reliable Merchant ID Issuance

- Centralized approval gate enforcing verification and evidence requirements.
- Approval receipts recording qualifying evidence and decisions.
- Idempotent simulated Merchant ID issuance.
- Recovery from lost issuer acknowledgements without generating a second Merchant ID.

## Getting Started

### Prerequisites

- Windows with PowerShell
- Python 3.11
- Git
- Tesseract OCR with English language data

Install Tesseract OCR if it is not already available:

```powershell
winget install --id UB-Mannheim.TesseractOCR -e
```

Ensure the Tesseract executable is accessible to the application.

### 1. Clone the repository

```powershell
git clone https://github.com/ayushgoel001/pinelabs-merchant-onboarding.git
cd pinelabs-merchant-onboarding
cd PineLabs-Case01-Starter
```

### 2. Create a virtual environment

```powershell
py -3.11 -m venv .venv
```

### 3. Install dependencies

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 4. Start the application

```powershell
.\.venv\Scripts\python.exe -m app.run --run-dir private/submission-demo --port 8000 --demo-controls
```

The launcher starts the FastAPI application and background worker as separate processes.

Open the Merchant Portal:

**http://127.0.0.1:8000/**

Press `Ctrl+C` in PowerShell to stop the application.

Restarting with the same run directory preserves the recorded application and workflow state.

## Accessing the Operator Desk

The platform provides separate merchant and operator experiences through the same application server.

**Merchant Portal**

Open `http://127.0.0.1:8000/` to submit applications, upload evidence, track verification progress, and respond to correction requests.

**Operator Desk**

1. Start the application using the command above.
2. Find the single-use operator authentication URL printed by the launcher.
3. The URL is also saved locally in:

   `private/submission-demo/operator-link.txt`

4. Open the operator URL in a separate browser profile to maintain independent merchant and operator sessions.
5. After authentication, access the review queue, application details, decisions, audit history, and operational metrics.

The Operator Desk does not require a separate server or startup command.

**Security note:** The operator authentication link is private and must not be committed or shared publicly.

## Demo Scenarios

The application includes synthetic scenarios for testing important onboarding workflows.

On the merchant application page, expand **Try a demo scenario** to select a predefined case.

| Scenario | Expected behavior |
|---|---|
| Successful onboarding | Required verifications qualify and a Merchant ID is issued. |
| Missing GST evidence | Application pauses until corrected evidence is provided, then verification resumes. |
| GST verification failure | Application requires correction or an operator decision; issuance remains blocked. |
| Risk referral | An operator reviews the referral and records a reasoned decision before approval. |
| Duplicate merchant | The application is held for review and no duplicate Merchant ID is issued. |
| Temporary provider failure | A retry is scheduled, and successful verification allows the workflow to continue. |
| Lost issuer acknowledgement | Issuance recovery reuses the existing operation key to recover the same Merchant ID. |

### Generate sample documents

To create sample evidence for the missing-tax scenario:

```powershell
.\.venv\Scripts\python.exe -m app.samples --out private/samples --scenario missing-tax
```

Upload the generated tax PDF through the merchant correction workflow.

Use a fresh run directory when repeating independent scenarios, since duplicate detection persists within a run.

**All demo data is synthetic. Do not upload real personal, financial, or government identity documents.**

## Operational Monitoring

The Operator Desk provides an operational dashboard showing:

- Total submitted applications and active merchants.
- Applications awaiting operator review.
- Approval rates and exception rates.
- Verification response times by provider.
- Retry attempts and duplicate callbacks.
- Dead-lettered jobs and onboarding completion times.

Metrics are computed from recorded application and workflow data.

## Testing

Run the automated test suite:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Run the repository privacy scanner:

```powershell
.\.venv\Scripts\python.exe tools/scan_privacy.py
```

Run the release checks:

```powershell
.\.venv\Scripts\python.exe tools/release_check.py
```

The tests cover document processing, access control, workflow state transitions, approval holds, signed callbacks, duplicate handling, background-worker recovery, and idempotent issuance.

### Browser Evidence

[View application screenshots and UI evidence](PineLabs-Case01-Starter/evidence/final-ui/README.md)

## Scope and Limitations

This project is an independent, local case-study implementation.

- PAN, GST, BANK, and RISK verification services are simulated.
- Merchant ID issuance is simulated.
- No production financial, banking, or government systems are connected.
- The application is intended for demonstration and evaluation on a trusted local environment, not production deployment.

The implementation demonstrates the architecture, reliability mechanisms, and operational workflows required for an auditable merchant onboarding system.

---

**Pine Labs Case Study 01 — Autonomous Merchant Onboarding Platform**
