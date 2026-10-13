"""Le workflow de la paire JSON JOUÉ DE BOUT EN BOUT : scripts d'étape RÉELS, CLI RÉEL, vrai ``GoogleUpstream``, vrai scanner (propriété C).

Rien n'est simulé de ce que C livre ni de ce que A livre : le YAML ``w5-json-pair.yml`` est lu, ses expressions ``${{ … }}`` sont résolues avec des
valeurs factices, ses conditions ``if`` sont évaluées (``success()`` implicite, ``always()``), chaque ``run:`` s'exécute dans ``bash -eo pipefail``
avec l'environnement que GitHub lui donnerait (``env:`` du job et de l'étape, ``$GITHUB_ENV`` rechargé entre les étapes), et les commandes
``python scripts/w5_json_pair.py …`` atteignent le VRAI CLI de A (``tests/w5_json_pair_cli_harness.py`` ne remplace que deux fabriques de
services externes : le dépôt GitHub de la fixture et la couche réseau de ``GoogleUpstream``). Les migrations, ``timeout``, ``env -u`` et
``scripts/w5_leak_scan.py`` sont les vrais. Seuls ``gh`` (pointe de ``main``) et ces deux services sont faux. Aucun réseau, aucune clé réelle.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from w5_json_pair_support import (
    FAKE_KEY,
    HOSTILE_KEY,
    http,
    ok,
    script,
)

from collegue.broker import json_pair

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "w5-json-pair.yml"
CLI = ROOT / "scripts" / "w5_json_pair.py"
HARNESS = ROOT / "tests" / "w5_json_pair_cli_harness.py"
CAMPAIGN = "w5-json-pair-001"
SHA = "c" * 40
FIXTURE_TOKEN = "ghs_FAKEfixtureTOKEN0123456789"
REPOSITORY = "VynoDePal/Collegue"

#: Valeurs que GitHub substituerait aux expressions du workflow (secrets factices).
SECRETS = {"W5_JSON_PAIR_GOOGLE_API_KEY": FAKE_KEY, "INTEGRATION_FIXTURE_GITHUB_TOKEN": FIXTURE_TOKEN}
#: Métadonnées d'un vrai runner : elles ressemblent à des secrets sans en être, le CLI doit les admettre.
RUNNER = {
    "RUNNER_OS": "Linux",
    "RUNNER_ARCH": "X64",
    "RUNNER_NAME": "GitHub Actions 7",
    "ACTIONS_RUNTIME_TOKEN": "runtime-metadata-not-a-model-key",
    "ACTIONS_RUNTIME_URL": "https://example.invalid/runtime",
    "GITHUB_ACTIONS": "true",
    "GITHUB_ACTOR": "VynoDePal",
    "GITHUB_WORKFLOW": "W5 JSON pair (comparaison ciblée)",
    "LANG": "C.UTF-8",
}


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["pair"]


# ── garde contre un contrôle final vert par absence d'un fichier ────────────────────────────────────────────────────────────────


def test_every_file_of_the_pair_exists_so_no_check_can_pass_by_skipping(workflow):
    """Un fichier supprimé doit rendre ce contrôle ROUGE, jamais un autre contrôle ignoré : aucun ``skip`` ne dépend de leur présence ici."""
    for path in (
        WORKFLOW,
        CLI,
        HARNESS,
        ROOT / "collegue" / "broker" / "json_pair.py",
        ROOT / "tests" / "test_w5_json_pair.py",
        ROOT / "tests" / "test_w5_json_pair_adversarial.py",
        ROOT / "tests" / "test_w5_json_pair_workflow.py",
    ):
        assert path.is_file(), path
    marker = "pytest." + "skip"
    for name in ("test_w5_json_pair_workflow.py", "test_w5_json_pair_workflow_cli.py"):
        text = (ROOT / "tests" / name).read_text(encoding="utf-8")
        assert marker not in text and "skip" + "if(" not in text, name


def test_the_cli_exposes_the_arbitrated_confirmation_the_workflow_requires():
    """La phrase n'existe qu'une fois (cœur A) : le CLI l'importe. On vérifie la valeur réellement exposée, pas une chaîne copiée dans le source."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("w5_json_pair_cli_probe", CLI)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    expected = "LANCER-UNE-PAIRE-JSON-2GENERATIONS-2USD-250000TOKENS-900S"
    assert cli.CONFIRMATION == json_pair.CONFIRMATION == expected
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert expected in workflow["jobs"]["pair"]["if"] and expected in str(workflow[True if True in workflow else "on"])
    parser = cli.build_parser()
    choices = next(a for a in parser._actions if a.dest == "command").choices
    assert sorted(choices) == ["claim", "finalize", "preflight", "run"]
    assert (cli.EXIT_OBSERVED, cli.EXIT_FAILED, cli.EXIT_INCOMPLETE) == (0, 1, 3)
    assert cli.REQUEST_SHA256 == json_pair.request_fingerprints()


# ── moteur : joue les étapes du YAML comme GitHub Actions ───────────────────────────────────────────────────────────────────────


def _resolve(text: str, *, inputs: dict, variables: dict, outcomes: dict) -> str:
    def substitute(match):
        key = match.group(1).strip()
        if key.startswith("inputs."):
            return inputs[key[7:]]
        if key.startswith("vars."):
            return variables.get(key[5:], "")
        if key.startswith("secrets."):
            return SECRETS[key[8:]]
        if key == "github.token":
            return "ephemeral-run-token-not-a-key"
        if key.startswith("steps.") and key.endswith(".outcome"):
            return outcomes.get(key[6:-8], "skipped")
        if key.startswith("env."):
            return "${ENV_" + key[4:] + "}"
        raise AssertionError(f"expression non prévue par le moteur de test : {key}")

    return re.sub(r"\$\{\{(.*?)\}\}", substitute, str(text))


def _condition(expression, outcomes: dict, failed: bool) -> bool:
    """Évalue les seules formes du workflow : ``always()``, ``steps.X.outcome == 'v'`` et ``&&`` (``success()`` implicite sans ``always()``)."""
    if expression is None:
        return not failed
    text = str(expression)
    always = "always()" in text
    for clause in (part.strip() for part in text.split("&&")):
        if clause == "always()":
            continue
        match = re.fullmatch(r"steps\.(\w+)\.outcome == '(\w+)'", clause)
        assert match, f"condition non prévue par le moteur de test : {clause}"
        if outcomes.get(match.group(1), "skipped") != match.group(2):
            return False
    return True if always else not failed


def _fake_gh(bindir: Path, tip: str) -> None:
    gh = bindir / "gh"
    gh.write_text(f"#!/bin/bash\nprintf '%s' '{tip}'\n", encoding="utf-8")
    gh.chmod(0o755)


def _shim(bindir: Path) -> None:
    """``python scripts/w5_json_pair.py …`` → doublure des seuls services externes ; tout le reste (migrations, scanner) est réel."""
    shim = bindir / "python"
    shim.write_text(
        "#!/bin/bash\n"
        # AUCUNE installation ni téléchargement dans ces tests : pip est refusé par le shim lui-même (défense en profondeur)
        'if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then echo "pip refusé par le banc de test" >&2; exit 97; fi\n'
        'if [ "$1" = "scripts/w5_json_pair.py" ]; then shift; exec "$REAL_PYTHON" "$HARNESS" "$@"; fi\n'
        'exec "$REAL_PYTHON" "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)


class Run:
    """Résultat d'un déroulé complet du workflow."""

    def __init__(self, root, outcomes, rcs, outputs, wire_log, call_log, claims, github_env, env):
        self.root, self.outcomes, self.rcs, self.outputs = root, outcomes, rcs, outputs
        self.wire_log, self.call_log, self.claims, self.github_env, self.env = (
            wire_log,
            call_log,
            claims,
            github_env,
            env,
        )

    @property
    def work(self) -> Path:
        return self.root / "runner" / "w5-json-pair"

    def published(self) -> list:
        publish = self.work / "publish"
        return (
            sorted(str(p.relative_to(publish)) for p in publish.rglob("*") if p.is_file()) if publish.exists() else []
        )

    def wire(self) -> list:
        if not self.wire_log.exists():
            return []
        return [json.loads(line) for line in self.wire_log.read_text().splitlines() if line]

    def calls(self) -> list:
        return self.call_log.read_text().splitlines() if self.call_log.exists() else []

    def everything(self) -> str:
        """Tout ce que le déroulé a affiché ou écrit hors du registre binaire : sorties des étapes, rapport du scanner, diagnostic."""
        parts = list(self.outputs.values())
        for name in ("leak-scan.json", "outcomes.json"):
            file = self.work / "diagnostic" / name
            if file.exists():
                parts.append(file.read_text())
        return "\n".join(parts)


def play(
    workflow: dict,
    tmp_path: Path,
    *,
    behaviors: dict | None = None,
    inputs: dict | None = None,
    variables: dict | None = None,
    context: dict | None = None,
    tip: str = SHA,
    spec: dict | None = None,
    reuse: Run | None = None,
    only: set | None = None,
) -> Run:
    root = reuse.root if reuse else tmp_path
    bindir = root / "bin"
    if not reuse:
        bindir.mkdir()
        _shim(bindir)
    _fake_gh(bindir, tip)
    wire_log, call_log, claims = root / "wire.jsonl", root / "calls.log", root / "claims.txt"
    spec_file = root / "harness.json"
    spec_file.write_text(
        json.dumps({"wire_log": str(wire_log), "call_log": str(call_log), "claims_file": str(claims),
                    "script": behaviors or script(), **(spec or {})})
    )  # fmt: skip
    inputs = {
        "confirm": json_pair.CONFIRMATION,
        "campaign_id": CAMPAIGN,
        "expected_sha": SHA,
        **(inputs or {}),
    }
    variables = {"INTEGRATION_E2E_ENABLED": "false", **(variables or {})}
    github_env_file = root / "github_env"
    if not reuse:
        github_env_file.write_text("")
    (root / "runner").mkdir(exist_ok=True)
    base = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "HOME": str(root),
        "REAL_PYTHON": sys.executable,
        "HARNESS": str(HARNESS),
        "W5_C_HARNESS": str(spec_file),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_NO_INDEX": "1",
        "RUNNER_TEMP": str(root / "runner"),
        "GITHUB_ENV": str(github_env_file),
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": SHA,
        "GITHUB_RUN_ID": "4242",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_REPOSITORY": REPOSITORY,
        **RUNNER,
        **(context or {}),
    }
    outcomes: dict = (
        dict(reuse.outcomes) if (reuse and only) else {}
    )  # étapes rejouées seules : issues des étapes précédentes conservées
    rcs: dict = {}
    outputs: dict = {}
    failed = False
    persisted: dict = {}
    if reuse:  # ``$GITHUB_ENV`` du déroulé précédent : les étapes rejouées voient les mêmes chemins
        persisted = dict(line.split("=", 1) for line in github_env_file.read_text().splitlines() if "=" in line)
    for step in workflow["steps"]:
        if "run" not in step:
            continue
        key = step.get("id") or step["name"]
        if only is not None and key not in only:
            continue
        if step["name"].startswith("Installer les dépendances verrouillées"):
            # l'installation (téléchargement) est EXCLUE du banc : seule sa forme est contrôlée (tests de structure)
            assert "pip install --require-hashes --no-deps -r locks/dev.txt" in step["run"]
            continue
        if not _condition(_resolve(step.get("if", "None"), inputs=inputs, variables=variables, outcomes=outcomes)
                          if "if" in step else None, outcomes, failed):  # fmt: skip
            outcomes[key] = "skipped"
            continue
        env = dict(base)
        env.update(persisted)
        for name, value in {**(workflow.get("env") or {}), **(step.get("env") or {})}.items():
            env[name] = _resolve(value, inputs=inputs, variables=variables, outcomes=outcomes)
        if "pair" == step.get("id"):
            env["PYTHONUNBUFFERED"] = "1"
        done = subprocess.run(
            ["bash", "-eo", "pipefail", "-c", step["run"]],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        outputs[key] = done.stdout + done.stderr
        rcs[key] = done.returncode
        outcomes[key] = "success" if done.returncode == 0 else "failure"
        failed = failed or done.returncode != 0
        for line in github_env_file.read_text().splitlines():
            if "=" in line:
                name, value = line.split("=", 1)
                persisted[name] = value
    return Run(root, outcomes, rcs, outputs, wire_log, call_log, claims, github_env_file, base)


def _report(run: Run) -> dict:
    return json.loads((run.work / "publish" / "report" / "json-pair.json").read_text())


def _registry_bytes(run: Run) -> bytes:
    """Octets du registre produit par le CLI ; un registre contaminé a été déplacé en quarantaine par le scanner (jamais supprimé)."""
    home = run.work / "home" / f"{CAMPAIGN}.sqlite3"
    if home.exists():
        return home.read_bytes()
    quarantined = [p for p in (run.work / "quarantine").rglob("*") if p.is_file()]
    return b"".join(p.read_bytes() for p in quarantined)


PAIR_FILES = [
    "registry/w5-json-pair-001.sqlite3",
    "report/json-pair.json",
    "report/json-pair.txt",
    "report/provenance.json",
]


# ── nominal ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────


def test_the_nominal_pair_runs_every_step_with_the_real_cli_and_publishes_only_the_scanned_set(workflow, tmp_path):
    run = play(workflow, tmp_path)
    assert run.outcomes == {
        "context": "success",
        "main_tip": "success",
        "preflight": "success",
        "claim": "success",
        "migrate": "success",
        "pair": "success",
        "Rapports sans clé (paire non lancée, aucune clé n'a existé)": "skipped",
        "Diagnostic minimal sûr (issues des étapes seulement)": "success",
    }, run.outputs
    assert "preflight: ok" in run.outputs["preflight"] and "claim: ok" in run.outputs["claim"]
    assert "status=observed reason=ok exit=0" in run.outputs["pair"]
    assert run.claims.read_text().split() == [f"collegue-business-claims/{CAMPAIGN}"], (
        "revendication créée une fois, jamais supprimée"
    )
    # les appels du CLI : jeton seulement à claim, clé seulement à run, finalize sans clé
    assert run.calls() == [
        "preflight LLM_API_KEY=absent GITHUB_TOKEN=absent",
        "claim LLM_API_KEY=absent GITHUB_TOKEN=present",
        "run LLM_API_KEY=present GITHUB_TOKEN=absent",
        "finalize LLM_API_KEY=absent GITHUB_TOKEN=absent",
    ]
    # le faux Google a reçu EXACTEMENT quatre requêtes, dans l'ordre, avec la clé en en-tête seulement et les corps attendus
    wire = run.wire()
    assert [(e["op"], e["variant"]) for e in wire] == [
        ("countTokens", "prompt_only"),
        ("generateContent", "prompt_only"),
        ("countTokens", "native_json"),
        ("generateContent", "native_json"),
    ]
    assert all(
        e["key_header_ok"]
        and not e["key_in_url"]
        and e["body_is_expected"]
        and e["host"] == "generativelanguage.googleapis.com"
        for e in wire
    )
    report = _report(run)
    assert (report["status"], report["reason"], report["business_validation"]) == ("observed", "ok", "not_executed")
    assert [v["json_valid"] for v in report["variants"]] == [True, True]
    assert report["budget"]["unknown_tokens"] == 0 and report["budget"]["blocked"] is False
    assert report["emissions"]["admitted"] == 2
    assert run.published() == PAIR_FILES
    provenance = json.loads((run.work / "publish" / "report" / "provenance.json").read_text())
    assert provenance["github"]["run_attempt"] == "1" and provenance["github"]["sha"] == SHA
    assert (
        provenance["claim_ref"] == f"collegue-business-claims/{CAMPAIGN}"
        and provenance["business_validation"] == "not_executed"
    )
    assert json.loads((run.work / "diagnostic" / "leak-scan.json").read_text())["verdict"] == "clean"
    assert FAKE_KEY not in run.everything()
    published = b"".join((run.work / "publish" / p).read_bytes() for p in run.published())
    assert FAKE_KEY.encode() not in published and FIXTURE_TOKEN.encode() not in published


def test_a_second_run_on_the_started_registry_reconciles_and_never_emits_again(workflow, tmp_path):
    first = play(workflow, tmp_path)
    assert first.rcs["pair"] == 0 and len(first.wire()) == 4
    for name in ("publish", "quarantine"):
        subprocess.run(["rm", "-rf", str(first.work / name)], check=True)
    # l'étape de paire seule rejouée (aucun rerun n'existe, mais la reprise doit rester sûre) : réconcilie, rapporte, refuse
    again = play(workflow, tmp_path, reuse=first, only={"pair"})
    assert again.rcs["pair"] == 3, again.outputs["pair"]
    assert "run: refused resume_refused" in again.outputs["pair"]
    assert len(again.wire()) == 4, "aucune requête de plus : une reprise réconcilie, rapporte et refuse"
    assert _report(again)["status"] == "observed", "le rapport relit l'état réel du registre"
    # un déroulé complet rejoué : le préflight refuse déjà un registre démarré, avant la revendication et avant la clé
    replay = play(workflow, tmp_path, reuse=first)
    assert replay.outcomes["preflight"] == "failure" and "refused registry_not_fresh" in replay.outputs["preflight"]
    assert replay.outcomes["claim"] == replay.outcomes["pair"] == "skipped" and len(replay.wire()) == 4


# ── refus avant la clé ────────────────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        (
            {"variables": {"INTEGRATION_E2E_ENABLED": ""}},
            "nightly_unestablished",
        ),  # variable absente ≠ preuve d'inactivité
        ({"variables": {"INTEGRATION_E2E_ENABLED": "true"}}, "nightly_active"),
        ({"variables": {"INTEGRATION_E2E_ENABLED": "False"}}, "nightly_unestablished"),
    ],
)
def test_the_real_nightly_variable_is_read_through_vars_and_anything_but_false_refuses_before_any_secret(
    workflow, tmp_path, kwargs, code
):
    run = play(workflow, tmp_path, **kwargs)
    assert run.outcomes["preflight"] == "failure" and f"refused {code}" in run.outputs["preflight"]
    assert run.outcomes["claim"] == run.outcomes["migrate"] == run.outcomes["pair"] == "skipped"
    assert run.claims.exists() is False and run.wire() == []
    assert "preflight LLM_API_KEY=absent GITHUB_TOKEN=absent" in run.calls() and not any(
        "claim" in c.split()[0] or c.startswith("run") for c in run.calls()
    )
    # la paire n'a pas démarré : l'étape de secours finalise sans clé et publie un rapport « non démarrée » scanné sans clé
    fallback = "Rapports sans clé (paire non lancée, aucune clé n'a existé)"
    assert run.outcomes[fallback] == "success"
    report = _report(run)
    assert (report["status"], report["reason"]) == ("incomplete", "not_started") and report["variants"][0][
        "state"
    ] == "not_started"
    assert run.published() == ["report/json-pair.json", "report/json-pair.txt", "report/provenance.json"]


def test_a_wrong_confirmation_or_a_moved_main_never_reaches_claim_or_the_key(workflow, tmp_path):
    moved = play(workflow, tmp_path / "moved", tip="d" * 40) if (tmp_path / "moved").mkdir() is None else None
    assert (
        moved.outcomes["main_tip"] == "failure"
        and moved.outcomes["preflight"] == "skipped"
        and moved.claims.exists() is False
    )
    # un SHA du run différent d'expected_sha est refusé par la première étape
    (tmp_path / "sha").mkdir()
    wrong = play(workflow, tmp_path / "sha", context={"GITHUB_SHA": "e" * 40})
    assert wrong.outcomes["context"] == "failure" and wrong.wire() == [] and wrong.claims.exists() is False
    # une relance (tentative 2) est refusée avant tout
    (tmp_path / "attempt").mkdir()
    rerun = play(workflow, tmp_path / "attempt", context={"GITHUB_RUN_ATTEMPT": "2"})
    assert rerun.outcomes["context"] == "failure" and rerun.calls() == []


def test_an_existing_claim_refuses_before_the_key_exists_and_the_reference_is_kept(workflow, tmp_path):
    run = play(workflow, tmp_path, spec={"existing": [f"collegue-business-claims/{CAMPAIGN}"]})
    assert run.outcomes["claim"] == "failure" and "claim: refused claim_exists" in run.outputs["claim"]
    assert run.outcomes["migrate"] == run.outcomes["pair"] == "skipped" and run.wire() == []
    assert not any(c.startswith("run") for c in run.calls())
    assert run.claims.exists() is False or run.claims.read_text() == "", (
        "la référence existante n'est ni recréée ni supprimée"
    )
    assert _report(run)["reason"] == "not_started"


@pytest.mark.parametrize("error", ["create_error", "read_error"])
def test_a_github_error_is_never_read_as_a_free_identity(workflow, tmp_path, error):
    run = play(workflow, tmp_path, spec={error: True})
    if error == "create_error":
        assert run.outcomes["claim"] == "failure" and "refused claim_unavailable" in run.outputs["claim"]
    else:  # lecture en erreur sans échec de création : la création exclusive a réussi, la preuve existe
        assert run.outcomes["claim"] == "success"
    assert run.outcomes["pair"] in ("skipped", "success")
    if error == "create_error":
        assert run.wire() == [] and run.outcomes["pair"] == "skipped"


# ── erreurs du fournisseur, verdicts, fuites ──────────────────────────────────────────────────────────────────────────────────


def test_an_ambiguous_native_error_keeps_the_unknown_and_reports_incomplete_with_code_three(workflow, tmp_path):
    run = play(workflow, tmp_path, behaviors=script(native_json=http(500, "INTERNAL")))
    assert run.rcs["pair"] == 3, run.outputs
    report = _report(run)
    assert (report["status"], report["reason"]) == ("incomplete", "upstream_ambiguous")
    assert report["budget"]["unknown_tokens"] == 270 and report["budget"]["blocked"] is True
    assert [v["state"] for v in report["variants"]] == ["observed", "unknown"] and report["variants"][1][
        "json_valid"
    ] is None
    assert [e["op"] for e in run.wire()] == ["countTokens", "generateContent", "countTokens", "generateContent"], (
        "aucun renvoi"
    )
    assert (
        run.published() == PAIR_FILES
        and json.loads((run.work / "diagnostic" / "leak-scan.json").read_text())["verdict"] == "clean"
    )
    assert HOSTILE_KEY not in run.everything() and FAKE_KEY not in run.everything()


def test_a_witness_error_stops_the_pair_before_the_native_variant(workflow, tmp_path):
    run = play(workflow, tmp_path, behaviors=script(prompt_only=http(500, "INTERNAL")))
    assert run.rcs["pair"] == 3
    assert [(e["op"], e["variant"]) for e in run.wire()] == [
        ("countTokens", "prompt_only"),
        ("generateContent", "prompt_only"),
    ]
    assert _report(run)["variants"][1]["state"] == "skipped" and _report(run)["budget"]["unknown_tokens"] == 270


def test_an_explicit_refusal_fails_the_pair_with_code_one_and_never_resends(workflow, tmp_path):
    run = play(workflow, tmp_path, behaviors=script(native_json=http(400, "INVALID_ARGUMENT")))
    assert run.rcs["pair"] == 1, run.outputs
    report = _report(run)
    assert (report["status"], report["reason"]) == ("failed", "upstream_rejected")
    assert len(run.wire()) == 4 and run.published() == PAIR_FILES


def test_a_native_non_json_response_is_failed_with_code_one_even_when_the_usage_is_settled(workflow, tmp_path):
    run = play(workflow, tmp_path, behaviors=script(native_json=ok("pas du JSON")))
    assert run.rcs["pair"] == 1, run.outputs
    report = _report(run)
    assert (report["status"], report["reason"]) == ("failed", "json_output_invalid")
    assert [v["json_valid"] for v in report["variants"]] == [True, False] and report["budget"]["unknown_tokens"] == 0


def test_a_non_json_witness_is_reported_and_the_second_variant_still_runs(workflow, tmp_path):
    run = play(workflow, tmp_path, behaviors=script(prompt_only=ok("```json\n{}\n```")))
    assert run.rcs["pair"] == 0, run.outputs
    report = _report(run)
    assert report["status"] == "observed" and [v["json_valid"] for v in report["variants"]] == [False, True]


def test_a_key_reflected_in_a_model_answer_is_judged_by_the_real_scanner_on_the_real_registry(workflow, tmp_path):
    """Le service reflète la clé dans sa sortie JSON : le rapport et le journal de la paire ne la contiennent pas (tests de A/B) ; le registre
    PEUT conserver le contenu du modèle. Le verdict dépend donc des octets réels du registre : si la clé y est, le scan est rouge et le
    registre n'est pas déposé ; sinon le scan est propre. Dans les deux cas la valeur n'est jamais publiée ni affichée."""
    run = play(workflow, tmp_path, behaviors=script(native_json=ok('{"ok": true, "echo": "' + FAKE_KEY + '"}')))
    in_registry = FAKE_KEY.encode() in _registry_bytes(run)
    scan = json.loads((run.work / "diagnostic" / "leak-scan.json").read_text())
    assert scan["verdict"] == ("leak" if in_registry else "clean"), (in_registry, scan, run.outputs)
    assert run.rcs["pair"] == (1 if in_registry else 0)
    assert ("registry/w5-json-pair-001.sqlite3" in run.published()) is (not in_registry)
    published = b"".join((run.work / "publish" / p).read_bytes() for p in run.published())
    assert FAKE_KEY.encode() not in published and FAKE_KEY not in run.everything()
    assert "report/json-pair.json" in run.published(), (
        "le rapport sain reste déposé ; seul le fichier contaminé est exclu"
    )


def test_a_key_reflected_in_the_registry_turns_the_run_red_and_the_registry_is_never_published(workflow, tmp_path):
    run = play(workflow, tmp_path, spec={"reflect_key_in_registry": True})
    assert FAKE_KEY.encode() in _registry_bytes(run), "précondition : les octets du registre portent la clé"
    assert run.rcs["pair"] == 1, run.outputs
    assert json.loads((run.work / "diagnostic" / "leak-scan.json").read_text())["verdict"] == "leak"
    assert run.published() == ["report/json-pair.json", "report/json-pair.txt", "report/provenance.json"]
    published = b"".join((run.work / "publish" / p).read_bytes() for p in run.published())
    assert FAKE_KEY.encode() not in published
    assert FAKE_KEY not in run.everything(), (
        "la valeur n'est ni affichée ni écrite, noms de fichiers du rapport du scanner compris"
    )
    # le fichier contaminé est en quarantaine, hors de l'ensemble déposé
    assert any((run.work / "quarantine").rglob("*")), "quarantaine non vide"


# ── reprise d'un journal FERMÉ par le vrai ``finalize`` (finding P2 de la PR #617, correctif A46) ───────────────────────────────
#
# Défaut établi par le manager : ``_conduct`` ferme le journal même quand une ÉCRITURE du service a échoué après l'émission (verrou SQLite, disque) ;
# la tentative reste ``emitting`` et la réserve ouverte, et l'ancien ``finalize`` prenait « journal fermé » pour « registre réparé ». Ce n'est PAS
# le scénario d'une annulation suivie d'une réparation en panne : là, le service a déjà marqué l'inconnue lui-même. Ici la panne tombe sur l'ÉCRITURE
# du marquage (``_block_unknown``) ou de l'opération du registre (``_apply_ledger``), dans le seul processus de ``run`` (doublure de banc) ; le
# ``finalize`` réel s'exécute ensuite dans un autre processus, sans clé, sans réseau et sans panne.


def _raw_state(database: Path) -> dict:
    """Observation indépendante du registre et du journal (SQLite en lecture seule)."""
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5.0)
    try:
        scope = connection.execute(
            "select consumed_tokens, reserved_tokens, unknown_tokens, blocked_reason from budget_scopes"
        ).fetchall()
        return {
            "journal": connection.execute("select state from w5_json_pair_run").fetchone()[0],
            "variants": connection.execute("select name, state from w5_json_pair_variant order by position").fetchall(),
            "attempts": [r[0] for r in connection.execute("select state from broker_attempts order by id")],
            "events": connection.execute("select kind, tokens from budget_events order by id").fetchall(),
            "consumed": sum(r[0] for r in scope),
            "reserved": sum(r[1] for r in scope),
            "unknown": sum(r[2] for r in scope),
            "blocked": any(r[3] for r in scope),
        }
    finally:
        connection.close()


def _cli(run: Run, command: str, *, key: bool = False) -> subprocess.CompletedProcess:
    """Une commande du VRAI CLI (via la doublure des services externes), avec l'environnement du runner et, pour ``run`` seulement, la clé."""
    env = dict(run.env)
    env["INTEGRATION_E2E_ENABLED"] = "false"
    if key:
        env["LLM_API_KEY"] = FAKE_KEY
    return subprocess.run(
        [sys.executable, str(HARNESS), command, "--root", str(run.work), "--campaign-id", CAMPAIGN],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _prepared(workflow, tmp_path, faults, behaviors=None) -> Run:
    """Étapes du workflow jusqu'aux migrations incluses ; la paire elle-même est lancée à la main pour observer l'état AVANT ``finalize``."""
    return play(
        workflow,
        tmp_path,
        behaviors=behaviors,
        spec={"faults": faults},
        only={"context", "main_tip", "preflight", "claim", "migrate"},
    )


def _db(run: Run) -> Path:
    return run.work / "home" / f"{CAMPAIGN}.sqlite3"


def test_finalize_repairs_a_closed_journal_whose_possible_emission_was_never_marked_unknown(workflow, tmp_path):
    run = _prepared(workflow, tmp_path, ["stale_repair", "block_unknown"], script(prompt_only=http(500, "INTERNAL")))
    conducted = _cli(run, "run", key=True)
    assert conducted.returncode == 3, conducted.stdout + conducted.stderr
    before = _raw_state(_db(run))
    # précondition : EXACTEMENT l'état du défaut, sinon le test ne prouve rien (une annulation donnerait déjà « unknown »)
    assert before["journal"] == "closed" and before["attempts"] == ["emitting"], before
    assert (before["reserved"], before["unknown"], before["blocked"]) == (270, 0, False), before
    requests = run.wire()
    assert [(e["op"], e["variant"]) for e in requests] == [
        ("countTokens", "prompt_only"),
        ("generateContent", "prompt_only"),
    ]

    first = _cli(run, "finalize")
    assert (first.returncode, first.stdout.strip()) == (3, "status=incomplete reason=upstream_ambiguous exit=3"), (
        first.stderr
    )
    repaired = _raw_state(_db(run))
    assert repaired["attempts"] == ["unknown"] and repaired["journal"] == "closed"
    assert (repaired["consumed"], repaired["reserved"], repaired["unknown"], repaired["blocked"]) == (0, 0, 270, True)
    report = json.loads((run.work / "report" / "json-pair.json").read_text())
    assert (report["status"], report["reason"]) == ("incomplete", "upstream_ambiguous")
    assert report["budget"] | {"deadline_at": None} == {
        "consumed_tokens": 0, "unknown_tokens": 270, "reserved_tokens": 0, "blocked": True, "deadline_at": None,
    }  # fmt: skip
    assert report["variants"][1]["state"] == "skipped" and report["business_validation"] == "not_executed"

    # idempotent : ni double comptage (270 une seule fois), ni écriture de plus, ni octet de rapport différent, ni requête fournisseur
    report_bytes = (run.work / "report" / "json-pair.json").read_bytes()
    for _ in range(2):
        again = _cli(run, "finalize")
        assert again.returncode == 3 and again.stdout == first.stdout
    final = _raw_state(_db(run))
    assert final == repaired and [e for e in final["events"] if e[0] == "unknown"] == [("unknown", 0)]
    assert (run.work / "report" / "json-pair.json").read_bytes() == report_bytes
    assert run.wire() == requests, "finalize n'émet, ne compte et ne génère jamais"
    assert run.calls()[-3:] == ["finalize LLM_API_KEY=absent GITHUB_TOKEN=absent"] * 3


def test_finalize_completes_a_durable_settlement_that_was_not_applied_to_the_ledger(workflow, tmp_path):
    run = _prepared(workflow, tmp_path, ["stale_repair", "apply_ledger_settled_once"])
    conducted = _cli(run, "run", key=True)
    assert conducted.returncode == 3, conducted.stdout + conducted.stderr
    before = _raw_state(_db(run))
    assert before["journal"] == "closed" and before["attempts"] == ["settled"], before
    assert (before["consumed"], before["reserved"], before["unknown"]) == (0, 270, 0), (
        "règlement établi, registre en retard"
    )
    requests = run.wire()

    first = _cli(run, "finalize")
    assert first.returncode == 3, "un règlement appliqué après coup ne fabrique pas un succès : l'arrêt reste incomplet"
    repaired = _raw_state(_db(run))
    assert (repaired["consumed"], repaired["reserved"], repaired["unknown"], repaired["blocked"]) == (37, 0, 0, False)
    assert repaired["attempts"] == ["settled"], "ni libéré à tort, ni converti en inconnue, usage mesuré et non estimé"
    report = json.loads((run.work / "report" / "json-pair.json").read_text())
    assert report["status"] == "incomplete" and report["status"] != "observed"
    assert report["budget"]["consumed_tokens"] == 37 and report["budget"]["reserved_tokens"] == 0
    assert report["variants"][0]["state"] == "observed" and report["variants"][1]["state"] != "observed"

    for _ in range(2):
        assert _cli(run, "finalize").returncode == 3
    assert _raw_state(_db(run)) == repaired, "idempotent : consommé 37 une seule fois"
    assert run.wire() == requests


@pytest.mark.parametrize(
    ("faults", "behaviors", "expected"),
    [
        (["stale_repair", "block_unknown"], script(prompt_only=http(500, "INTERNAL")), (0, 0, 270, True)),
        (["stale_repair", "apply_ledger_settled_once"], script(), (37, 0, 0, False)),
    ],
)
def test_the_pair_step_publishes_the_registry_repaired_by_finalize_never_the_one_left_by_the_failed_write(
    workflow, tmp_path, faults, behaviors, expected
):
    """Dans le workflow réel : ``run`` (avec la panne), puis ``finalize`` sans clé, puis le scan. Le registre déposé est celui que ``finalize`` a RÉPARÉ."""
    run = play(workflow, tmp_path, behaviors=behaviors, spec={"faults": faults})
    assert run.rcs["pair"] == 3, run.outputs["pair"]
    assert [c.split()[0] for c in run.calls() if c.split()[0] in ("run", "finalize")] == ["run", "finalize"]
    published = run.work / "publish" / "registry" / f"{CAMPAIGN}.sqlite3"
    state = _raw_state(published)
    assert (state["consumed"], state["reserved"], state["unknown"], state["blocked"]) == expected, state
    report = _report(run)
    assert report["status"] == "incomplete" and report["budget"]["reserved_tokens"] == 0
    assert json.loads((run.work / "diagnostic" / "leak-scan.json").read_text())["verdict"] == "clean"
    assert run.published() == PAIR_FILES and FAKE_KEY not in run.everything()
