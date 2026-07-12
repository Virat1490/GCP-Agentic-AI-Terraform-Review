# Agentic AI Terraform Review

An automated pipeline that policy-checks Terraform infrastructure against custom GCP security/compliance rules, then uses an LLM (Gemini) to explain each violation and propose a fix — posted as a Markdown PR comment.

## What's in this repo

| File | Role |
|---|---|
| `main.tf` | Terraform configuration defining GCP resources (the thing being checked) |
| `gcp_rules.rego` | OPA (Open Policy Agent) policy — the rules Terraform must satisfy |
| `agent_review.py` | Orchestrator — parses OPA's violations, locates the offending code, asks Gemini for a fix, and builds the final report |

These three files work together as stages of one pipeline, not as independent scripts. `agent_review.py` is the only piece you actually execute; the other two are its inputs (indirectly, via files it expects on disk: `tfplan.json` and `opa_violations.json`).

## How it works, end to end

```
                                main.tf
                                    │
                                    ▼
                                terraform plan
                                    │
                                    ▼
                                tfplan.json
                                    │
                                    ▼
                         opa eval (gcp_rules.rego)
                                    │
                                    ▼
                          opa_violations.json
                                    │
                                    ▼
                           agent_review.py
                          ┌─────────┴─────────┐
                          │                   │
                 find_resource_code()   get_ai_fix() → Gemini API
                          │                   │
                          └─────────┬─────────┘
                                    ▼
                            pr_comment.txt
                          (posted to GitHub PR)
```

1. **Terraform plans the infrastructure.** `terraform plan -out=tfplan.binary && terraform show -json tfplan.binary > tfplan.json` turns `main.tf` into a JSON plan describing every resource that will be created, changed, or destroyed.

2. **OPA evaluates the plan against policy.** `opa eval` runs `gcp_rules.rego` against `tfplan.json` and writes its result to `opa_violations.json`. The rego file defines three `deny` rules under `package terraform.gcp`:
   - Every `google_storage_bucket` must have `uniform_bucket_level_access = true`.
   - Every resource's `region` must be `us-central1`.
   - No `google_compute_instance` may have a network interface with `access_config` (i.e. no public IP).
   
   Each rule that fails emits a human-readable message string, e.g. `"security-risk: Bucket 'test_public_bucket' must have uniform bucket level access enabled."`

3. **`agent_review.py` reads and parses the violations.** `generate_review()` digs into OPA's nested JSON output (`result[0].expressions[0].value`) to pull out the list of violation strings. If there are none, it returns a short "all clear" message and stops.

4. **It cross-references the Terraform plan to identify resource types.** For each violation message it regex-extracts the resource name (the text in quotes, e.g. `test_public_bucket`) and looks up that resource's `type` (e.g. `google_storage_bucket`) from `tfplan.json`'s `resource_changes` array.

5. **It locates the actual offending code.** `find_resource_code()` scans every `*.tf` file in the search path with a regex matching `resource "<type>" "<name>" {`, then does manual brace-counting to extract the complete resource block — a lightweight substitute for a real HCL parser.

6. **It asks Gemini to explain and fix the violation.** If a `GEMINI_API_KEY` environment variable is set and the `google-generativeai` package is installed, `get_ai_fix()` sends the violation message plus the extracted code to `gemini-1.5-pro` with a prompt asking it to (a) explain the risk and (b) return corrected HCL. If the API key or SDK is missing, or the call fails, it falls back to a manual "please fix this yourself" note that still includes the original code block.

7. **It assembles a Markdown report.** Per violation: a heading with the resource name, its category and message, the source file, and either the AI's analysis/fix or the manual fallback. The whole thing is joined into one Markdown document.

8. **Output is written for CI.** The script writes the final report to `pr_comment.txt`, which a GitHub Action step would typically post as a comment on the pull request.

## Why this design

- **OPA does policy, not opinions.** Rego rules are declarative and easy to audit/version — `agent_review.py` never re-implements policy logic, it only *reports on* OPA's verdict.
- **Regex + brace-counting instead of a real HCL parser.** This keeps the script dependency-free but is a known fragility point (see Limitations).
- **Gemini is optional, not required.** `HAS_GENAI` and the `GEMINI_API_KEY` check mean the script degrades gracefully to a manual-fix message if AI isn't configured — the pipeline never silently fails just because a key is missing.
- **Resource-name matching is done via regex on the violation message**, not passed as structured data from OPA — so the `rego` messages must consistently wrap the resource name in single quotes for this to work.

## Setup

```bash
# 1. Terraform & provider
terraform init

# 2. OPA (Open Policy Agent) — https://www.openpolicyagent.org/docs/latest/#running-opa
brew install opa   # or download a release binary

# 3. Python deps (only needed for the AI-fix step)
pip install google-generativeai

# 4. Gemini API key (optional — enables AI-generated fixes)
export GEMINI_API_KEY="your-key-here"
```

## Usage

```bash
# Generate the Terraform plan as JSON
terraform plan -out=tfplan.binary
terraform show -json tfplan.binary > tfplan.json

# Evaluate policy — emits opa_violations.json
opa eval --input tfplan.json --data gcp_rules.rego \
  "data.terraform.gcp.deny" --format json > opa_violations.json

# Run the reviewer
python agent_review.py
# → prints the Markdown report and writes pr_comment.txt
```

In a GitHub Actions workflow, these same steps run in CI, and a follow-up step (e.g. `peter-evans/create-or-update-comment`) posts `pr_comment.txt` to the pull request.

## Expected result against the included `main.tf`

The sample `main.tf` is deliberately seeded with violations, so a run should report:

1. **`test_public_bucket`** — `uniform_bucket_level_access = false` → security-risk violation.
2. **`bad_subnet`** — `region = "europe-west1"` → compliance (region) violation.
3. **`vulnerable_vm`** — has an `access_config {}` block → public-IP security-risk violation (and likely a region violation too, depending on how `zone` vs `region` is evaluated by the rego rule).

`compliant_bucket` should pass cleanly and generate no violation.

## Limitations / things to watch

- **Brace-counting HCL extraction** in `find_resource_code()` will break on braces inside strings or comments (e.g. `# uses {curly} braces`) — it's a heuristic, not a real parser.
- **Resource-name regex matching** assumes OPA messages always wrap the name in single quotes; changing the rego `sprintf` format would break the report.
- **The "region" rule** in `gcp_rules.rego` checks `resource.change.after.region`, which many GCP resources (like compute instances, which use `zone` instead) won't have — worth double-checking that rule actually fires the way the `main.tf` comments assume.
- **No retry/backoff** around the Gemini call — a transient API error just falls back to the manual message.
- **`api_key` is read once per script run**, not per violation, so it's efficient, but there's no per-call error surfacing beyond a printed exception.

## Suggested next steps

- Swap the brace-counting parser for a proper HCL parser (e.g. `python-hcl2`) for robustness.
- Have the rego rules emit structured JSON (resource name *and* type) instead of a formatted string, removing the need for regex extraction and the `resource_map` cross-reference.
- Add unit tests for `find_resource_code` and `generate_review` using the sample `main.tf`/`opa_violations.json` in this repo as fixtures.
