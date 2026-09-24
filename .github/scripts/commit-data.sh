#!/usr/bin/env bash
# Commit changes under data/ and push, rebasing and retrying if another
# workflow pushed in the meantime. The jobs write different files, so a
# rebase never conflicts.
set -euo pipefail

message="$1"
branch="${GITHUB_REF_NAME:?}"

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

git add data/
if git diff --cached --quiet; then
  echo "No data changes to commit."
  exit 0
fi
git commit -m "$message"

for attempt in 1 2 3 4; do
  if git push origin "HEAD:$branch"; then
    exit 0
  fi
  echo "Push rejected (attempt $attempt); rebasing onto the latest $branch and retrying."
  sleep $((2 ** attempt))
  git pull --rebase origin "$branch"
done

echo "::error::Could not push data changes after 4 attempts."
exit 1
