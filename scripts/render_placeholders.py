"""Substitute __path/to/file__ placeholders in a manifest with that file's contents.

The mock-presidio ConfigMap embeds two files (the analyzer and its vocabulary) as
indented block scalars. Keeping them as real files on disk — rather than pasted
into the YAML — is what lets `guardrail/test_mock_presidio.py` import the same code
the cluster runs, and what makes the vocab drift test meaningful.

    python3 scripts/render_placeholders.py manifests/60-mock-presidio.yaml | kubectl apply -f -

A placeholder is the repo-relative path wrapped in double underscores, on a line of
its own, e.g. `__guardrail/mock_presidio.py__`. The path must stay inside the repo.
Lines are indented to the placeholder's own indentation, so the result is valid YAML
block-scalar content; blank lines stay blank rather than becoming whitespace.
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PLACEHOLDER = re.compile(r"^(\s*)__([A-Za-z0-9._/-]+)__\s*$", re.MULTILINE)


def resolve(name):
    path = (BASE / name).resolve()
    if not path.is_relative_to(BASE):
        sys.exit(f"placeholder __{name}__ escapes the repo: {path}")
    if not path.is_file():
        sys.exit(f"placeholder __{name}__ has no file: expected {path}")
    return path


def render(text):
    def substitute(match):
        indent = match.group(1)
        body = resolve(match.group(2)).read_text(encoding="utf-8").rstrip("\n")
        return "\n".join(f"{indent}{line}" if line.strip() else "" for line in body.split("\n"))

    rendered, count = PLACEHOLDER.subn(substitute, text)
    return rendered, count


def main():
    args = [a for a in sys.argv[1:] if a != "-q"]
    if len(args) != 1:
        sys.exit(__doc__)
    rendered, count = render(Path(args[0]).read_text(encoding="utf-8"))
    # Most manifests have no placeholders; only a file that was expected to have them
    # makes this warning useful. -q silences it for bulk loops.
    if not count and "-q" not in sys.argv[1:]:
        print(f"warning: no placeholders in {args[0]}", file=sys.stderr)
    sys.stdout.write(rendered)


if __name__ == "__main__":
    main()
