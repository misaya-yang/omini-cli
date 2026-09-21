#!/usr/bin/env bash
#
# One-shot setup for a machine that has just cloned this repository.
#
#   ./scripts/bootstrap.sh                          # set up and verify
#   ./scripts/bootstrap.sh --project my-project     # also run the probe
#
# It checks each prerequisite in the order it can actually fail, says what is
# missing in plain words, and stops rather than half-working. Nothing here needs
# a credential file: authentication is one interactive `gcloud` login, and the
# program reads whatever that leaves behind.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PROJECT="${GOOGLE_CLOUD_PROJECT:-}"
SKIP_DOCTOR=0

while [ $# -gt 0 ]; do
  case "$1" in
    --project) PROJECT="$2"; shift 2 ;;
    --skip-doctor) SKIP_DOCTOR=1; shift ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
step() { printf '\n\033[1m%s\033[0m\n' "$1"; }

fail() {
  echo
  echo "Stopped: $1"
  [ -n "${2:-}" ] && echo "$2"
  exit 1
}

# ── 1. Python ────────────────────────────────────────────────────────────────
step "1. Python"

PY=""
for candidate in python3.13 python3.12 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    version="$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)"
    if [ -n "$version" ] && [ "$(printf '%s\n3.12\n' "$version" | sort -V | head -1)" = "3.12" ]; then
      PY="$candidate"; ok "$candidate (Python $version)"; break
    fi
  fi
done

if [ -z "$PY" ]; then
  bad "no Python 3.12+ found"
  fail "Python 3.12 or newer is required." \
       "Install it (brew install python@3.12, or https://python.org), then re-run."
fi

if command -v uv >/dev/null 2>&1; then
  ok "uv $(uv --version | awk '{print $2}')"
  HAVE_UV=1
else
  warn "uv not found; falling back to venv + pip (slower, same result)"
  HAVE_UV=0
fi

# ── 2. Virtual environment ──────────────────────────────────────────────────
step "2. Virtual environment"

if [ -d .venv ]; then
  ok ".venv already exists"
else
  if [ "$HAVE_UV" = "1" ]; then
    uv venv --python 3.12 >/dev/null 2>&1 || fail "uv venv failed"
  else
    "$PY" -m venv .venv || fail "venv creation failed"
  fi
  ok "created .venv"
fi

# ── 3. Dependencies ─────────────────────────────────────────────────────────
step "3. Installing dependencies"

if [ "$HAVE_UV" = "1" ]; then
  uv pip install -e ".[dev]" >/dev/null 2>&1 || fail "dependency install failed (run it without >/dev/null to see why)"
else
  ./.venv/bin/pip install -q -e ".[dev]" || fail "dependency install failed"
fi
ok "installed omni-homevlog and its dependencies"

CLI="./.venv/bin/omni-vlog"
[ -x "$CLI" ] || fail "the omni-vlog command was not created; the install did not complete"
ok "omni-vlog is on the venv path"

# ── 4. ffmpeg (optional) ───────────────────────────────────────────────────
step "4. Optional tools"

if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
  ok "ffmpeg present: keyframe review and transcoding available"
else
  warn "ffmpeg not found"
  echo "      Without it, the Critic reviews keyframes only if they can be"
  echo "      extracted — so it will report a degraded review and the decision"
  echo "      policy will refuse to auto-accept. Everything else still works."
  echo "      Install with: brew install ffmpeg"
fi

# ─ 5. gcloud ───────────────────────────────────────────────────────────────
step "5. Google Cloud CLI"

if command -v gcloud >/dev/null 2>&1; then
  ok "gcloud present"
else
  bad "gcloud not found"
  fail "The Google Cloud CLI is needed once, to create Application Default Credentials." \
       "Install: https://cloud.google.com/sdk/docs/install"
fi

# ── 6. Credentials ──────────────────────────────────────────────────────────
step "6. Application Default Credentials"

# An array, not `${PROJECT:+--project "$PROJECT"}`. The latter expands to a single
# argument containing a space — `--project my-project` as one word — which the CLI
# cannot parse, so the check reported "no usable ADC" on a machine where ADC worked.
DOCTOR_ARGS=()
[ -n "$PROJECT" ] && DOCTOR_ARGS+=(--project "$PROJECT")

# Capture, then match. `doctor | grep -q` looks equivalent and is not: `grep -q`
# closes the pipe as soon as it matches, the CLI dies of SIGPIPE, and `pipefail`
# then reports the *pipeline* as failed — so a working machine read as "no usable
# ADC". The fix is to let the command finish before looking at its output.
doctor_output="$("$CLI" doctor "${DOCTOR_ARGS[@]}" 2>&1 || true)"

if printf '%s\n' "$doctor_output" | grep -q "^Auth *PASS"; then
  ok "ADC works on this machine"
else
  bad "no usable ADC"
  echo
  echo "  Run one of these, then run this script again:"
  echo
  echo "    gcloud auth application-default login"
  echo
  echo "    # if your own account lacks Vertex access, impersonate the service"
  echo "    # account that has it:"
  echo "    gcloud auth application-default login \\"
  echo "      --impersonate-service-account=<service-account>@<project>.iam.gserviceaccount.com"
  echo
  exit 1
fi

# ── 7. Project ──────────────────────────────────────────────────────────────
step "7. Project"

if [ -z "$PROJECT" ]; then
  warn "GOOGLE_CLOUD_PROJECT is not set"
  echo "      Set it and re-run, or pass --project:"
  echo "        export GOOGLE_CLOUD_PROJECT=<your project id>"
  echo "        ./scripts/bootstrap.sh --project <your project id>"
  echo
  echo "      (It is not a secret. It identifies which project to bill and"
  echo "       which quota to draw on.)"
  exit 0
fi
ok "project: $PROJECT"

# ── 8. Remember it ──────────────────────────────────────────────────────────
step "8. Configuration"

if [ -f .env ]; then
  if grep -q "^GOOGLE_CLOUD_PROJECT=$PROJECT$" .env 2>/dev/null; then
    ok ".env already names this project"
  else
    warn ".env exists but does not name $PROJECT"
    echo "      Update GOOGLE_CLOUD_PROJECT in .env, or pass --project each time."
  fi
else
  cp .env.example .env
  if sed --version >/dev/null 2>&1; then
    sed -i "s|^GOOGLE_CLOUD_PROJECT=.*|GOOGLE_CLOUD_PROJECT=$PROJECT|" .env
  else
    sed -i '' "s|^GOOGLE_CLOUD_PROJECT=.*|GOOGLE_CLOUD_PROJECT=$PROJECT|" .env
  fi
  ok "created .env with your project id"
  echo "      .env is gitignored. It holds configuration, not credentials."
fi

# ─ 9. Verify ───────────────────────────────────────────────────────────────
if [ "$SKIP_DOCTOR" = "1" ]; then
  echo
  echo "Skipped the probe. Run it yourself with:"
  echo "  ./.venv/bin/omni-vlog doctor --project $PROJECT"
  exit 0
fi

step "9. Probing the surface"
echo "  (free checks only — generation probes cost money and are opt-in)"
echo
"$CLI" doctor --project "$PROJECT"
status=$?

cat <<EOF

────────────────────────────────────────────────────────────────────────
Set up. Everyday use, from this directory:

  ./.venv/bin/omni-vlog --help

Or activate the environment and drop the path:

  source .venv/bin/activate      # Windows: .venv\\Scripts\\activate
  omni-vlog --help

Read docs/SETUP.md before moving to a third machine, and docs/LIMITATIONS.md
before trusting any capability: most of this project is exercised against
fakes, and that document says exactly which parts are not.
EOF

exit $status