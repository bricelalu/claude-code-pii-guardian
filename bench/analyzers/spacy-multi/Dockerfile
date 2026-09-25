# Benchmark-only analyzer: stock Presidio with per-language spaCy pipelines
# (en/fr/es/it). The benchmark sends each file's known language, which the real
# gateway can't do, so this variant's results are an upper bound.
# Build context: bench/analyzers
ARG PRESIDIO_ANALYZER_IMAGE=mcr.microsoft.com/presidio-analyzer:latest@sha256:286e3fa7f3a7426e775e8564fe1870f1ba8f999d3ab8bbb8cc46a44355d9d6e9
FROM ${PRESIDIO_ANALYZER_IMAGE}

# The base image runs as a non-root user (1001); install system-wide, then drop back.
USER root
COPY spacy-multi/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-deps --require-hashes -r /tmp/requirements.txt

COPY spacy-multi/nlp.yaml /app/bench_nlp.yaml
COPY common/merge_registry.py spacy-multi/recognizers.extra.yaml /tmp/
RUN python /tmp/merge_registry.py presidio_analyzer/conf/default_recognizers.yaml \
      /tmp/recognizers.extra.yaml /app/bench_recognizers.yaml \
 && python -c "import yaml; c = yaml.safe_load(open('presidio_analyzer/conf/default_analyzer.yaml')) or {}; c['supported_languages'] = ['en', 'fr', 'es', 'it']; yaml.safe_dump(c, open('/app/bench_analyzer.yaml', 'w'))"

ENV NLP_CONF_FILE=/app/bench_nlp.yaml \
    RECOGNIZER_REGISTRY_CONF_FILE=/app/bench_recognizers.yaml \
    ANALYZER_CONF_FILE=/app/bench_analyzer.yaml

USER 1001
