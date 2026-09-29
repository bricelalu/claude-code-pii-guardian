"""Offline consistency checks across manifests/, Taskfile.yml and guardrail/.

Drift here is silent by nature. Nothing fails when a ConfigMap and the repo disagree;
the proxy just keeps running the file it was given last, or CrashLoopBackOffs on an
import. These checks make the wiring wrong *before* it is applied.

The four invariants, each of which has actually broken this project:

  1. Every subPath a Deployment mounts is a key some ConfigMap in manifests/ declares.
     Otherwise the pod waits forever in ContainerCreating on
     "MountVolume.SetUp failed: configmap not found" — and only if the ConfigMap is
     missing from the wrong namespace, which is silent success.
  2. Every guardrail in the LiteLLM config that names a local module is mounted. A
     `guardrail: leak_guard.LeakGuard` that LiteLLM cannot use is *also* silent: the
     class imports, it just is not a CustomGuardrail, so no hook ever fires.
  3. Rendered guardrail ConfigMap keys are byte-identical to the repo files, so the
     code under test is the code that runs.
  4. The Taskfile creates no ConfigMaps imperatively. `kubectl create configmap
     --from-file` has no manifest, so `kubectl diff` cannot see it and git cannot track
     it; that is how /app/code_guard.py went stale while the repo moved on.

    python3 scripts/test_manifest_consistency.py
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render_placeholders import render  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
MANIFESTS = sorted((REPO / "manifests").glob("*.yaml"))
GUARDRAIL_CONFIGMAPS = ("litellm-guardrail", "litellm-leak-guard")


def load_yaml():
    # Named explicitly because the failure mode is otherwise a bare traceback from deep
    # inside a test, and the fix is not obvious: on a PEP-668 system Python, `pip install
    # pyyaml` is rejected, so the interpreter that has it is not the default one. A task
    # whose documented command does not run is worse than one that is slow.
    try:
        import yaml
    except ImportError:
        raise ImportError(
            "pyyaml is required by the manifest-consistency checks but is not installed "
            "for this interpreter.\n"
            "  These checks are deliberately NOT skipped when it is missing: a subPath "
            "with no ConfigMap key, or a pasted-in copy of a guardrail file, is a silent "
            "failure that only shows up later as a pod stuck in ContainerCreating.\n"
            "  Fix:  task test PYTHON=/path/to/python-with-pyyaml\n"
            "     or: PYTHON=/path/to/python-with-pyyaml task test\n"
            "     or: python3 -m venv .venv && .venv/bin/pip install pyyaml"
        )
    return yaml


def parsed_manifests():
    """Every manifest as a list of documents, with __path__ placeholders resolved."""
    yaml = load_yaml()
    out = []
    for path in MANIFESTS:
        text, _ = render(path.read_text())
        out.append((path.name, [d for d in yaml.safe_load_all(text) if d]))
    return out


def configmaps():
    """(namespace, name) -> set of data keys, from every manifest."""
    found = {}
    for _, docs in parsed_manifests():
        for d in docs:
            if d.get("kind") == "ConfigMap":
                found[(d["metadata"].get("namespace"), d["metadata"]["name"])] = set(
                    (d.get("data") or {}).keys())
    return found


def deployments():
    for name, docs in parsed_manifests():
        for d in docs:
            if d.get("kind") == "Deployment":
                yield name, d


def litellm_config():
    """The config.yaml embedded in 21-litellm-config.yaml, parsed."""
    yaml = load_yaml()
    for _, docs in parsed_manifests():
        for d in docs:
            if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config":
                return yaml.safe_load(d["data"]["config.yaml"])
    raise AssertionError("no litellm-config ConfigMap in manifests/")


class SubPathHasAKeyTest(unittest.TestCase):
    """Invariant 1 — a mounted subPath must exist in the ConfigMap it names."""

    def test_every_mounted_subpath_is_provided(self):
        cms = configmaps()
        problems = []
        for manifest, dep in deployments():
            ns = dep["metadata"].get("namespace")
            spec = dep["spec"]["template"]["spec"]
            vols = {v["name"]: v for v in spec.get("volumes", [])}
            for container in spec["containers"]:
                for mount in container.get("volumeMounts", []):
                    sub = mount.get("subPath")
                    if not sub:
                        continue  # whole-volume mount: no key needed
                    vol = vols.get(mount["name"], {})
                    if "configMap" not in vol:
                        continue  # secret/emptyDir: not ours to check
                    key = (ns, vol["configMap"]["name"])
                    if key not in cms:
                        problems.append(
                            f"{manifest}: {dep['metadata']['name']} mounts {sub} from "
                            f"ConfigMap {vol['configMap']['name']}, which no manifest declares")
                    elif sub not in cms[key]:
                        problems.append(
                            f"{manifest}: {dep['metadata']['name']} mounts subPath {sub} but "
                            f"ConfigMap {vol['configMap']['name']} only provides "
                            f"{sorted(cms[key])}")
        self.assertEqual(problems, [], "\n".join(problems))


class GuardrailIsMountedTest(unittest.TestCase):
    """Invariant 2 — a configured guardrail must be a file the pod actually has."""

    def test_every_local_guardrail_is_mounted(self):
        litellm = [d for _, d in deployments() if d["metadata"]["name"] == "litellm"]
        self.assertEqual(len(litellm), 1, "expected exactly one litellm Deployment")
        spec = litellm[0]["spec"]["template"]["spec"]
        mounted = {m["mountPath"].rsplit("/", 1)[-1]
                   for c in spec["containers"] for m in c.get("volumeMounts", [])
                   if m.get("subPath")}
        for g in litellm_config().get("guardrails", []):
            path = g.get("litellm_params", {}).get("guardrail", "")
            if "." not in path:
                continue  # a built-in name, not a dotted path
            module = path.rpartition(".")[0]
            if f"{module}.py" in mounted:
                continue  # ours, and present
            self.assertNotIn(
                module, mounted,
                f"config guardrail {path} names a local module, but no {module}.py is "
                f"mounted into the litellm pod")

    def test_guardrail_order_is_documented_as_load_bearing(self):
        """LeakGuard completes a column only from a token already in it.

        Run before code-guard it has nothing to key on and silently completes nothing,
        so the config order is a correctness requirement, not a style choice. If someone
        reorders the list, make them re-read why.
        """
        names = [g["guardrail_name"] for g in litellm_config().get("guardrails", [])]
        if "leak-guard" in names and "code-guard" in names:
            self.assertLess(names.index("code-guard"), names.index("leak-guard"),
                            f"code-guard must precede leak-guard; got {names}")


class RenderedBytesMatchRepoTest(unittest.TestCase):
    """Invariant 3 — the cluster must run the repo's bytes, not a hand-edited copy."""

    def test_guardrail_configmaps_match_their_repo_files(self):
        for _, docs in parsed_manifests():
            for d in docs:
                if d.get("kind") != "ConfigMap" or d["metadata"]["name"] not in GUARDRAIL_CONFIGMAPS:
                    continue
                for key, body in d["data"].items():
                    src = REPO / "guardrail" / key
                    self.assertTrue(src.is_file(), f"{key} has no file at guardrail/{key}")
                    self.assertEqual(
                        body.rstrip("\n"), src.read_text().rstrip("\n"),
                        f"ConfigMap {d['metadata']['name']} key {key} does not match guardrail/{key}")

    def test_every_guardrail_file_is_a_configmap_key(self):
        """A new module under guardrail/ that nobody mounts is a latent silent drop."""
        keys = set()
        for _, docs in parsed_manifests():
            for d in docs:
                if d.get("kind") == "ConfigMap" and d["metadata"]["name"] in GUARDRAIL_CONFIGMAPS:
                    keys |= set(d["data"].keys())
        # The files the proxy loads. Anything else in guardrail/ is a test, an entry
        # point, or the failing-masker fixture and is not meant to be mounted.
        self.assertLessEqual({"code_guard.py", "leak_guard.py", "leak_guard_hook.py"}, keys)


class NoImperativeConfigmapsTest(unittest.TestCase):
    """Invariant 4 — one mechanism for config, so nothing can be created off-manifest."""

    def test_taskfile_never_creates_a_configmap(self):
        taskfile = (REPO / "Taskfile.yml").read_text()
        offenders = [ln.strip() for ln in taskfile.splitlines()
                     # Skip comments: this file explains why the command is banned, and
                     # prose about it must not read as a violation.
                     if not ln.lstrip().startswith("#")
                     and re.search(r"kubectl create configmap", ln)]
        self.assertEqual(
            offenders, [],
            "Taskfile creates ConfigMaps imperatively, which leaves them invisible to "
            "`kubectl diff` and to git. Declare them in manifests/ instead and apply "
            "through scripts/render_placeholders.py.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
