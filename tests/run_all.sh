#!/bin/sh
# Run every tests/test_*.py; exit non-zero if any of them fails.
cd "$(dirname "$0")/.." || exit 1
fail=0
for t in tests/test_*.py; do
  if out=$(python3 "$t" 2>&1); then
    echo "PASS  $t"
  else
    echo "FAIL  $t"; echo "$out" | tail -20 | sed 's/^/      /'; fail=1
  fi
done
exit $fail
