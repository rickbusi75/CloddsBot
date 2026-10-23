# Build in an isolated public context containing this file and ops/autonomy/worker.py.
# Inherit the deployed dist, dependencies and Metaculus overlay without rebuilding them.
FROM lab/cloddsbot:rick-20261023.1
COPY worker.py /app/ops/autonomy/worker.py
LABEL io.rick.digest.owner="research-hub"
