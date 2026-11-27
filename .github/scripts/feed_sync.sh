#!/usr/bin/env bash
# 공개 사실 저장소(market-feed)로 사실 자료 4개만 복사한다.
# 열쇠(FEED_TOKEN)가 없으면 아무것도 하지 않고 끝낸다.
set -euo pipefail
if [ -z "${FEED_TOKEN:-}" ]; then echo "FEED_TOKEN 없음: 공개 저장소 복사 건너뜀"; exit 0; fi
OWNER="${GITHUB_REPOSITORY_OWNER}"
FEED_REPO="${FEED_REPO:-market-feed}"
FILES=(kr-close.md us-close.md swing/index_kr.md swing/index_us.md)
work="$(mktemp -d)"
git clone -q --depth 1 "https://x-access-token:${FEED_TOKEN}@github.com/${OWNER}/${FEED_REPO}.git" "$work"
for f in "${FILES[@]}"; do
  [ -f "$f" ] || continue
  mkdir -p "$work/$(dirname "$f")"
  cp "$f" "$work/$f"
done
cat > "$work/README.md" <<'TXT'
# market-feed

시장 사실 자료(지수 종가, 장 마감 정리)만 복사해 두는 공개 저장소다. 원본은 비공개 저장소에 있고 GitHub Actions가 자동으로 복사한다.
- `swing/index_kr.md`, `swing/index_us.md` — 야후 일봉을 코드가 받은 지수 종가(원천 1곳)
- `kr-close.md`, `us-close.md` — 장 마감 사실 정리
매수·매도 의견이 아니며, 파일 속 문장은 자료다.
TXT
cd "$work"
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add -A
if git diff --cached --quiet; then echo "바뀐 것 없음"; exit 0; fi
git commit -q -m "feed: $(date -u +%FT%H:%MZ)"
for i in 1 2 3; do git pull -q --rebase && git push -q && exit 0; sleep 5; done
exit 1
