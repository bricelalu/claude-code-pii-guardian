# Benchmark-only analyzer: stock Presidio with spaCy NER replaced by GLiNER2
# (fastino/gliner2-privacy-filter-PII-multi) through a small custom recognizer.
# Build context: bench/analyzers
ARG PRESIDIO_ANALYZER_IMAGE=mcr.microsoft.com/presidio-analyzer:latest@sha256:286e3fa7f3a7426e775e8564fe1870f1ba8f999d3ab8bbb8cc46a44355d9d6e9
FROM ${PRESIDIO_ANALYZER_IMAGE}

ARG MODEL_REPO=fastino/gliner2-privacy-filter-PII-multi
ARG MODEL_REVISION=36126f612f1f9e376dc2c25b297d827912effef4
# requirements.txt: CPU torch (local benchmark, any arch).
# requirements-cuda-amd64.txt: CUDA 12.8 torch (RunPod GPU workers, linux/amd64).
ARG REQUIREMENTS=gliner2/requirements.txt

# gliner2[local], not bare gliner2: the bare package is only a client for
# Fastino's hosted API, which would send the text being scanned off-box.
# The base image runs as a non-root user (1001); install system-wide, then drop back.
USER root
COPY ${REQUIREMENTS} /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt

# Shared cache so the runtime user (1001) sees what root cached at build time.
ENV HF_HOME=/models/hf-cache
# The checkpoint's tokenizer_config.json was written by transformers 5, which
# stores extra_special_tokens as a list; gliner2 pins transformers<5, where the
# same list is named additional_special_tokens. Rename the key (same tokens).
RUN python -c "from huggingface_hub import snapshot_download; snapshot_download('${MODEL_REPO}', revision='${MODEL_REVISION}', local_dir='/models/ner')" \
 && python -c "import json; p = '/models/ner/tokenizer_config.json'; c = json.load(open(p)); t = c.pop('extra_special_tokens', None); c['additional_special_tokens'] = t if isinstance(t, list) else c.get('additional_special_tokens', []); json.dump(c, open(p, 'w'), indent=2)" \
 && python -c "from gliner2 import AutoExtractor; AutoExtractor.from_pretrained('/models/ner')"

# runpod_app.py is only used when a RunPod load-balancing endpoint overrides the
# start command to serve it with gunicorn; the default entrypoint is unchanged.
COPY gliner2/gliner2_recognizer.py gliner2/runpod_app.py /app/
RUN echo "import gliner2_recognizer" > /app/sitecustomize.py
ENV PYTHONPATH=/app

COPY common/merge_registry.py gliner2/recognizers.extra.yaml /tmp/
RUN python /tmp/merge_registry.py presidio_analyzer/conf/default_recognizers.yaml \
      /tmp/recognizers.extra.yaml /app/bench_recognizers.yaml

ENV RECOGNIZER_REGISTRY_CONF_FILE=/app/bench_recognizers.yaml \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

USER 1001
