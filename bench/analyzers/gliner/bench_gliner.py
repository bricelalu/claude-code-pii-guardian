"""Presidio's built-in GLiNERRecognizer with the benchmark's settings baked in.

Presidio's YAML schema only keeps known fields for predefined recognizers and
silently drops the rest (model_name, entity_mapping, ...), so the settings are
fixed here. Imported at startup via sitecustomize.py; the registry YAML then
enables `BenchGLiNERRecognizer` by name.
"""
from presidio_analyzer.predefined_recognizers import GLiNERRecognizer

ENTITY_MAPPING = {
    "person": "PERSON",
    "name": "PERSON",
    "location": "LOCATION",
    "city": "LOCATION",
    "country": "LOCATION",
    "address": "LOCATION",
}


class BenchGLiNERRecognizer(GLiNERRecognizer):
    def __init__(self, supported_language: str = "en", name: str = "BenchGLiNERRecognizer", **kwargs):
        super().__init__(
            name=name,
            supported_language=supported_language,
            model_name="/models/ner",
            entity_mapping=ENTITY_MAPPING,
            flat_ner=False,
            multi_label=True,
            threshold=0.3,
            map_location="cpu",
            **kwargs,
        )

    # The stock recognizer also appends every requested Presidio entity
    # (PHONE_NUMBER, URL, CREDIT_CARD, ...) as zero-shot labels. Query only the
    # mapped NER labels so this variant differs from spaCy in NER alone, like gliner2.
    def _GLiNERRecognizer__create_input_labels(self, entities):
        return list(ENTITY_MAPPING)
