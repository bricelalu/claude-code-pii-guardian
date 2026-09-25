# Benchmark-only analyzer: stock Presidio with spaCy NER replaced by GLiNER v1
# (urchade/gliner_multi_pii-v1) through Presidio's built-in GLiNERRecognizer.
# Build context: bench/analyzers
ARG PRESIDIO_ANALYZER_IMAGE=mcr.microsoft.com/presidio-analyzer:latest@sha256:286e3fa7f3a7426e775e8564fe1870f1ba8f999d3ab8bbb8cc46a44355d9d6e9
FROM ${PRESIDIO_ANALYZER_IMAGE}

ARG MODEL_REPO=urchade/gliner_multi_pii-v1
ARG MODEL_REVISION=1fcf13e85f4eef5394e1fcd406cf2ca9ea82351d

# The base image runs as a non-root user (1001); install system-wide, then drop back.
USER root
COPY gliner/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt

# Weights (and the mdeberta backbone GLiNER resolves by name) are fetched at
# build time only; the running container is offline.
# Shared cache so the runtime user (1001) sees what root cached at build time.
ENV HF_HOME=/models/hf-cache
RUN python -c "from huggingface_hub import snapshot_download; snapshot_download('${MODEL_REPO}', revision='${MODEL_REVISION}', local_dir='/models/ner')" \
 && python -c "from gliner import GLiNER; GLiNER.from_pretrained('/models/ner', map_location='cpu')"

COPY gliner/bench_gliner.py /app/bench_gliner.py
RUN echo "import bench_gliner" > /app/sitecustomize.py
ENV PYTHONPATH=/app

COPY common/merge_registry.py gliner/recognizers.extra.yaml /tmp/
RUN python /tmp/merge_registry.py presidio_analyzer/conf/default_recognizers.yaml \
      /tmp/recognizers.extra.yaml /app/bench_recognizers.yaml

ENV RECOGNIZER_REGISTRY_CONF_FILE=/app/bench_recognizers.yaml \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

USER 1001
