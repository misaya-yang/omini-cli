# Setting this up on another machine

You need **three values** and **one login**. Nothing else travels between
machines, and in particular no key file does.

```text
GOOGLE_CLOUD_PROJECT    your Google Cloud project id
the service account     the identity that has Vertex access, if your own
                        user account does not
a GCS bucket prefix     optional; needed for GCS delivery and for strategy C
```

Keep those three somewhere you can read them — a note, a password manager, an
email to yourself. They are **not secrets**. A project id is an identifier, and a
service-account address is public information; what grants access is the IAM
binding on the project, not knowledge of the name.

## What you must NOT copy across

| item | why |
|---|---|
| `application_default_credentials.json` | It is a refresh token tied to one machine's session. Copying it is worse than re-running one command, which takes ten seconds. |
| Any service-account JSON key | The whole design avoids these. If IT hands you one as a last resort, keep it outside the repository, `chmod 600` it, and point `GOOGLE_APPLICATION_CREDENTIALS` at it — never commit it. |
| `.env` | It may carry a `GEMINI_API_KEY`. Recreate it with `cp .env.example .env`, which is two values you already have. |
| `.omni-vlog/` | Per-machine job state. Jobs do not move between machines; that is a separate design question, not a copy. |

## Steps

### 1. Get the code

```bash
git clone git@github.com:misaya-yang/omini-cli.git
cd omini-cli
```

### 2. Python environment

Python 3.12 or newer. `uv` is fastest if you have it:

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
```

Without `uv`:

```bash
python3.12 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

### 3. Authenticate

This is the step that replaces "copying a key file". It is per-machine and
short-lived by design.

```bash
gcloud auth application-default login
```

If your user account does not itself have Vertex access, impersonate the service
account that does:

```bash
gcloud auth application-default login \
  --impersonate-service-account=<service-account>@<project>.iam.gserviceaccount.com
```

The program never runs `gcloud`. It reads whatever ADC points at, once, and
refreshes tokens on demand.

### 4. Configure

```bash
cp .env.example .env
```

Then set, in `.env` or in your shell:

```bash
GOOGLE_CLOUD_PROJECT=<your project id>
GOOGLE_CLOUD_LOCATION=global
OMNI_VERTEX_MODEL=gemini-omni-1.1-flash-preview

# Optional. Without it, output is delivered inline as base64, which is fine for
# a first look and is not what you want for a 30-second chain.
# OMNI_OUTPUT_GCS_URI=gs://<approved-bucket>/omni-output/
```

### 5. Verify

```bash
omni-vlog doctor --project "$GOOGLE_CLOUD_PROJECT"
```

`Auth` and `Endpoint reachable` should both be PASS. Every generation row says
SKIPPED, because those cost money — that is the intended default, not a failure.

Only when you want to measure what the surface can actually do:

```bash
RUN_LIVE_VIDEO_TESTS=1 omni-vlog doctor \
  --project "$GOOGLE_CLOUD_PROJECT" --run-generation
```

That makes up to four paid calls at 3s / 360p.

## IAM

The minimum is small, and it is worth asking for exactly this rather than
something broader:

| role | scope | why |
|---|---|---|
| `roles/aiplatform.user` | project | invoke the model |
| `roles/storage.objectCreator` | the output bucket | write renders, if you use GCS delivery |
| `roles/storage.objectViewer` | the output bucket | read them back |
| `roles/iam.serviceAccountTokenCreator` | the service account | only if you impersonate one |

Do not ask for Owner, Editor, or Storage Admin. Nothing here needs them, and
`docs/RECOVERY.md` explains the failure each missing role produces.

## When something is wrong

| symptom | cause |
|---|---|
| `missing_credentials` | ADC is not set up on this machine. Step 3. |
| `permission_denied` (403) | The identity lacks `roles/aiplatform.user`, or has no access to the bucket. |
| `quota_exhausted` (429) | The project has no usable quota for this Preview model. This is an entitlement fact, not a code defect — do not work around it by rotating keys or projects. |
| `Not logged into any GitHub hosts` | Only affects pushing, not running. |

`omni-vlog doctor` prints the remediation for each of these alongside the failure.

## Notes on cost

`pricing.yaml` ships with zeroed rates, because the real numbers could not be
verified at build time. Every cost figure the tool prints is an **estimate**; the
manifest records token counts, so a reconciliation against Cloud Billing is
possible. Nothing here reads your billing account.