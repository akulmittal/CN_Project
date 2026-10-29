#!/usr/bin/env bash
set -euo pipefail
BASE="${BASE:-http://localhost/backend/api/v1}"
printf '\n1) Health\n'
curl -fsS "$BASE/health"; echo
printf '\n2) Login admin (prompts only if you edit this script)\n'
echo 'Use the README curl examples to login, create an exam, add a question, publish, and test student access.'
