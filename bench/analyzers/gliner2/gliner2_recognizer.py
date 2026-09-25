"""Presidio recognizer backed by a local GLiNER2 model (gliner2[local]).

Presidio's built-in GLiNERRecognizer targets the `gliner` package and can't load
GLiNER2 checkpoints. Presidio resolves YAML `type: predefined` names against
every imported EntityRecognizer subclass, so importing this module at startup
(see sitecustomize.py) is enough to make `GLiNER2Recognizer` available.

Settings live here, not in the registry YAML: Presidio's YAML schema only keeps
known fields for predefined recognizers and silently drops the rest.
"""
from typing import Dict, List, Optional

from presidio_analyzer import LocalRecognizer, RecognizerResult

MODEL_PATH = "/models/ner"
THRESHOLD = 0.3
ENTITY_MAPPING: Dict[str, str] = {
    "person": "PERSON",
    "full_name": "PERSON",
    "first_name": "PERSON",
    "last_name": "PERSON",
    "address": "LOCATION",
    "street_address": "LOCATION",
    "city": "LOCATION",
    "state_or_region": "LOCATION",
    "country": "LOCATION",
}


class GLiNER2Recognizer(LocalRecognizer):
    def __init__(self, supported_language: str = "en", name: str = "GLiNER2Recognizer", **kwargs):
        self.model = None
        super().__init__(
            supported_entities=sorted(set(ENTITY_MAPPING.values())),
            supported_language=supported_language,
            name=name,
            **kwargs,
        )

    def load(self) -> None:
        import torch
        from gliner2 import AutoExtractor

        device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = AutoExtractor.from_pretrained(MODEL_PATH, map_location=device)

    def analyze(self, text: str, entities: List[str], nlp_artifacts=None) -> Optional[List[RecognizerResult]]:
        labels = [label for label, entity in ENTITY_MAPPING.items() if entity in entities]
        if not labels:
            return []
        out = self.model.extract_entities_long(
            text, labels, threshold=THRESHOLD, include_confidence=True, include_spans=True
        )
        results = []
        for label, hits in out.get("entities", {}).items():
            for hit in hits:
                results.append(RecognizerResult(
                    entity_type=ENTITY_MAPPING[label],
                    start=hit["start"],
                    end=hit["end"],
                    score=float(hit["confidence"]),
                ))
        return results
