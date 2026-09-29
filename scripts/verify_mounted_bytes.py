"""Assert the running pod is executing the guardrail code the repo describes.

`task config-drift` compares each manifest to the cluster, but that comparison stops at
the ConfigMap. It cannot see the file the container is actually running, and the hop in
between is where this project's config has silently failed twice.

Kubernetes writes a subPath-mounted ConfigMap file once, when the container starts, and
never refreshes it. So `kubectl apply` of a changed ConfigMap is accepted, `kubectl diff`
reports the cluster as matching the manifests, the guardrail still imports, and the pod
carries on running the previous version. Every signal says the change is live.

Seen for real here: a LeakGuard fix was in the repo and in the ConfigMap while the pod
still ran the pre-fix file, and a request through the gateway produced exactly the leak
the fix removed. Nothing errored at any point, which is what makes it worth a check that
looks at hashes instead of load state.

    python3 scripts/verify_mounted_bytes.py            # exits non-zero if anything is stale
    python3 scripts/verify_mounted_bytes.py --verbose

Pairing: repo == ConfigMap is covered by scripts/test_manifest_consistency.py.
ConfigMap == running pod is this file. `task reload-config` closes the gap by restarting
the deployment, which is the only thing that re-reads a subPath mount.
"""
import argparse
import hashlib
import os
import subprocess
import sys

# Set from --context before any kubectl call. A one-slot holder rather than a global
# string, so run() stays a plain function and stays testable.
CONTEXT = [""]

# Read straight from the Deployment rather than hardcoding, so a rename in
# manifests/22-litellm.yaml cannot leave this checking a file that is no longer mounted.
DEFAULT_MOUNTS = [
    ("/app/code_guard.py", "litellm-guardrail"),
    ("/app/leak_guard.py", "litellm-leak-guard"),
    ("/app/leak_guard_hook.py", "litellm-leak-guard"),
]

EMPTY_SHA = hashlib.sha256(b"").hexdigest()


def run(args, namespace):
    """kubectl with the namespace placed after the subcommand verb.

    Placement matters twice over. Appending -n to the end puts it *after* any `--`, which
    hands it to the remote command instead of kubectl — `sha256sum /app/x.py -n gateway`
    — and kubectl then reports NotFound for the deployment. Putting it before the verb is
    not valid either. So: verb first, then -n, then everything else.
    """
    verb, *rest = args
    cmd = ["kubectl"]
    if CONTEXT[0]:
        cmd += ["--context", CONTEXT[0]]
    cmd += [verb, "-n", namespace, *rest]
    return subprocess.run(cmd, capture_output=True, text=True)


def pod_hash(path, namespace):
    """sha256 of a file as the running container sees it."""
    proc = run(["exec", "deploy/litellm", "--", "sha256sum", path], namespace)
    if proc.returncode != 0:
        return None, proc.stderr.strip()[:200]
    return proc.stdout.split()[0], None


def configmap_key(name, key, namespace):
    """One key's contents, or None.

    `-o jsonpath={.data.code_guard.py}` cannot be used: jsonpath reads the dots in a key
    as a field path, so a key like code_guard.py yields the empty string with no error.
    Parsing the JSON avoids that, and go-template is no better — the guardrail sources
    contain "{{" and "}}", which it then tries to evaluate.
    """
    import json
    proc = run(["get", "configmap", name, "-o", "json"], namespace)
    if proc.returncode != 0:
        return None, proc.stderr.strip()[:200]
    data = json.loads(proc.stdout).get("data") or {}
    return data.get(key), None


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", "--namespace", default="gateway")
    ap.add_argument("--context", default=os.environ.get("KUBE_CONTEXT", ""),
                    help="passed through to kubectl; empty means the current context")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    CONTEXT[0] = args.context

    problems = 0
    for path, cm in DEFAULT_MOUNTS:
        name = path.rsplit("/", 1)[-1]
        live, err = pod_hash(path, args.namespace)
        if live is None:
            print(f"  MISSING  {name}: {err}")
            problems += 1
            continue

        body, err = configmap_key(cm, name, args.namespace)
        if body is None:
            # A key that is absent and a key that is empty string are different failures;
            # the first means the ConfigMap is wrong, the second that it is blank.
            print(f"  ERROR    {name}: not found in ConfigMap {cm}"
                  + (f" ({err})" if err else ""))
            problems += 1
            continue

        want = sha(body)
        if live == want:
            print(f"  ok       {name}  {live[:16]}")
        elif live == EMPTY_SHA:
            print(f"  EMPTY    {name}: mounted but zero bytes")
            problems += 1
        else:
            print(f"  STALE    {name}: pod {live[:16]} != {cm} {want[:16]}")
            if args.verbose:
                print(f"           the pod is running code that predates the current "
                      f"{cm}; a subPath mount only re-reads on restart")
            problems += 1

    if problems:
        print()
        print(f"✗ {problems} file(s) not current — run 'task reload-config', which "
              f"restarts the deployment because a subPath mount")
        print("  does not pick up a ConfigMap change on its own.")
        return 1
    print("✓ the pod is running the guardrail code the ConfigMaps describe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
