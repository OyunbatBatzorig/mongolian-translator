#!/bin/bash
# Runs the nllb_expanded training config, auto-resuming from the latest
# checkpoint if the process gets killed (this environment has killed
# background training across session boundaries more than once).
set -o pipefail
cd "$(dirname "$0")/.."
source venv/bin/activate

OUT_DIR="benchmark/nllb_expanded"
for attempt in 1 2 3 4 5; do
    LATEST=$(ls -d "$OUT_DIR"/checkpoint-* 2>/dev/null | sort -t- -k2 -n | tail -1)
    if [ -n "$LATEST" ]; then
        echo "=== Attempt $attempt: resuming from $LATEST ==="
        PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True python3 benchmark/train_bench.py nllb_expanded "$LATEST"
    else
        echo "=== Attempt $attempt: starting fresh ==="
        PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True python3 benchmark/train_bench.py nllb_expanded
    fi
    STATUS=$?
    if [ $STATUS -eq 0 ]; then
        echo "=== Training completed successfully ==="
        exit 0
    fi
    echo "=== Attempt $attempt failed with status $STATUS, retrying ==="
done
echo "=== Gave up after 5 attempts ==="
exit 1
