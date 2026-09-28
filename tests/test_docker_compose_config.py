"""
Tests unitaires pour la configuration Docker Compose.
Vérifie que le kc-provisioner est désactivé et que le healthcheck est correct.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


class TestDockerComposeConfig:
    """Tests pour la configuration docker-compose.yml."""

    @pytest.fixture
    def compose_file(self):
        """Charge le fichier docker-compose.yml."""
        compose_path = os.path.join(os.path.dirname(__file__), "..", "docker-compose.yml")
        with open(compose_path, "r") as f:
            return yaml.safe_load(f)

    def test_docker_compose_syntax_is_valid(self, compose_file):
        """Test que le fichier docker-compose.yml est syntaxiquement valide."""
        assert compose_file is not None
        assert "services" in compose_file
        assert "collegue-app" in compose_file["services"]

    def test_no_custom_network_defined(self, compose_file):
        """Test qu'aucun réseau custom n'est défini (utilise le réseau par défaut de Coolify)."""
        # Pas de networks section, ou si elle existe, pas de collegue-network
        # car Coolify gère son propre réseau et Caddy utilise {{upstreams}}
        if "networks" in compose_file:
            assert "collegue-network" not in compose_file.get("networks", {}), (
                "collegue-network should not be defined - let Coolify manage the network"
            )

    def test_services_use_default_network(self, compose_file):
        """Test que les services n'ont pas de réseau explicite (utilisent le réseau par défaut)."""
        for service_name in ["collegue-app", "nginx", "keycloak"]:
            service = compose_file["services"].get(service_name, {})
            # Les services ne doivent pas avoir de networks explicitement défini
            assert "networks" not in service, (
                f"{service_name} should not have explicit networks - use Coolify's default network"
            )

    def test_healthcheck_port_is_correct(self, compose_file):
        """Test que le healthcheck pointe sur le bon port (4122 pour health_server)."""
        collegue_app = compose_file["services"]["collegue-app"]
        healthcheck = collegue_app.get("healthcheck", {})
        test_command = healthcheck.get("test", [])

        # Vérifier que la commande healthcheck existe
        assert test_command, "Healthcheck test command not found"

        # Trouver l'URL dans la commande
        healthcheck_url = None
        for item in test_command:
            if isinstance(item, str) and "http://localhost" in item:
                healthcheck_url = item
                break

        assert healthcheck_url is not None, "Healthcheck URL not found in test command"
        # Le health_server écoute sur 4122 (voir entrypoint.sh et health_server.py)
        assert ":4122/" in healthcheck_url, f"Healthcheck should use port 4122, found: {healthcheck_url}"

    def test_kc_provisioner_is_disabled(self, compose_file):
        """Test que le service kc-provisioner est désactivé/commenté."""
        services = compose_file.get("services", {})

        # Le service ne devrait pas être présent ou être commenté (donc pas parsé par YAML)
        assert "kc-provisioner" not in services, "kc-provisioner service should be disabled (commented out)"

    def test_keycloak_service_still_present(self, compose_file):
        """Test que Keycloak est toujours présent (au cas où on veut l'utiliser plus tard)."""
        services = compose_file.get("services", {})
        assert "keycloak" in services, "Keycloak service should still be present"

        keycloak = services["keycloak"]
        assert keycloak.get("image", "").startswith("quay.io/keycloak/"), "Keycloak should use the correct image"

    def test_nginx_depends_on_app(self, compose_file):
        """Test que nginx dépend bien de collegue-app."""
        nginx = compose_file["services"].get("nginx", {})
        depends_on = nginx.get("depends_on", {})

        assert "collegue-app" in depends_on, "nginx should depend on collegue-app"

        # Vérifier que la condition est bien sur le healthcheck
        if isinstance(depends_on, dict):
            condition = depends_on.get("collegue-app", {}).get("condition", "")
            assert condition == "service_healthy", f"nginx should wait for collegue-app to be healthy, got: {condition}"

    def test_collegue_app_ports_exposed(self, compose_file):
        """Test que les ports MCP sont exposés directement (contournement Traefik)."""
        collegue_app = compose_file["services"]["collegue-app"]
        ports = collegue_app.get("ports", [])

        # Vérifier que les ports sont exposés
        port_mappings = []
        for port in ports:
            if isinstance(port, str):
                port_mappings.append(port)
            elif isinstance(port, dict):
                port_mappings.append(f"{port.get('published')}:{port.get('target')}")

        # Port 4121 (MCP) doit être exposé
        assert any("4121" in p for p in port_mappings), "MCP port 4121 should be exposed"
        # Port 4122 (health) doit être exposé
        assert any("4122" in p for p in port_mappings), "Health port 4122 should be exposed"

    def test_collegue_app_environment_variables(self, compose_file):
        """Test que les variables d'environnement essentielles sont présentes."""
        collegue_app = compose_file["services"]["collegue-app"]
        env = collegue_app.get("environment", {})

        required_env = ["PORT", "MCP_TRANSPORT", "MCP_HOST", "PYTHONUNBUFFERED"]
        for var in required_env:
            assert var in env, f"Required environment variable {var} not found"

        # Vérifier que le port est bien 4121 (peut être int ou string selon le parsing YAML)
        port_value = env.get("PORT")
        assert str(port_value) == "4121", f"PORT should be 4121, got: {port_value} (type: {type(port_value)})"


class TestDockerComposeHealthcheckIntegration:
    """Tests d'intégration pour le healthcheck."""

    def test_healthcheck_matches_app_port(self):
        """Test que le port du healthcheck correspond au health_server (port 4122)."""
        compose_path = os.path.join(os.path.dirname(__file__), "..", "docker-compose.yml")

        with open(compose_path, "r") as f:
            content = f.read()

        # Extraire le port du healthcheck
        import re

        healthcheck_match = re.search(r"http://localhost:(\d+)/_health", content)
        assert healthcheck_match, "Could not find healthcheck URL pattern"

        healthcheck_port = int(healthcheck_match.group(1))

        # Le healthcheck doit être sur 4122 (health_server), pas 4121 (MCP server)
        assert healthcheck_port == 4122, f"Healthcheck port {healthcheck_port} should be 4122 (health_server)"


COMPOSE_PATH = Path(__file__).resolve().parents[1] / "docker-compose.yml"
PUBLISH_VAR = "COLLEGUE_PUBLISH_HOST"
# Surface MCP (OAuth-protégée quand activée) : MCP direct, health, et nginx qui ne proxifie que /mcp/,
# /_health et /.well-known/. Le dashboard (sans authentification) et Keycloak (start-dev) ont chacun
# leur variable : exposer le MCP ne doit jamais ouvrir ces deux-là.
EXPECTED_BINDINGS = {
    "collegue-app": ("COLLEGUE_PUBLISH_HOST", {4121, 4122}),
    "nginx": ("COLLEGUE_PUBLISH_HOST", {8088}),
    "collegue-dashboard": ("COLLEGUE_DASHBOARD_PUBLISH_HOST", {4125}),
    "keycloak": ("COLLEGUE_KEYCLOAK_PUBLISH_HOST", {4123}),
}
LOOPBACK_PORT = re.compile(r"^\$\{(COLLEGUE_[A-Z_]*PUBLISH_HOST):-127\.0\.0\.1\}:(\d+):\d+$")
_INTERPOLATION = re.compile(r"\$\{(\w+):-([^}]*)\}")


def _compose() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def _published_ports() -> dict[str, list[str]]:
    services = _compose()["services"]
    return {
        name: [str(p) for p in service.get("ports", [])] for name, service in services.items() if "ports" in service
    }


def _docker_compose_available() -> bool:
    return (
        shutil.which("docker") is not None
        and subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0
    )


def _rendered_host_ips(tmp_path: Path, **env_overrides: str) -> dict[int, str | None]:
    """{port publié: host_ip} d'après `docker compose config` (résolution réelle des variables)."""

    shutil.copy(COMPOSE_PATH, tmp_path / "docker-compose.yml")
    (tmp_path / ".env").write_text("", encoding="utf-8")  # env_file requis, contenu vide
    env = {key: value for key, value in os.environ.items() if "PUBLISH_HOST" not in key}
    env.update(env_overrides)
    completed = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    rendered = json.loads(completed.stdout)
    return {
        int(port["published"]): port.get("host_ip")
        for service in rendered["services"].values()
        for port in service.get("ports", [])
    }


class TestComposePublishesOnLoopbackByDefault:
    """Profil local : aucun port hôte n'est joignable depuis le réseau sans choix explicite.

    Le conteneur continue d'écouter sur 0.0.0.0 EN INTERNE (entrypoint.sh, MCP_HOST) ; seule la
    PUBLICATION hôte est restreinte, via des variables COLLEGUE_*_PUBLISH_HOST (défaut 127.0.0.1).
    """

    def test_the_five_known_host_publications_are_all_covered(self):
        published = {int(LOOPBACK_PORT.match(m).group(2)) for mappings in _published_ports().values() for m in mappings}

        assert published == {4121, 4122, 4123, 4125, 8088}

    def test_every_published_port_defaults_to_loopback_through_its_variable(self):
        ports = _published_ports()

        assert set(ports) == set(EXPECTED_BINDINGS)
        for service, mappings in ports.items():
            variable, expected_ports = EXPECTED_BINDINGS[service]
            bound = set()
            for mapping in mappings:
                match = LOOPBACK_PORT.match(mapping)
                assert match, f"{service}: '{mapping}' doit être publié via ${{COLLEGUE_*_PUBLISH_HOST:-127.0.0.1}}"
                assert match.group(1) == variable, (service, mapping)
                bound.add(int(match.group(2)))
            assert bound == expected_ports, service

    def test_default_resolution_is_loopback_and_override_is_possible(self):
        def resolve(mapping: str, env: dict[str, str]) -> str:
            return _INTERPOLATION.sub(lambda m: env.get(m.group(1), m.group(2)), mapping)

        for service, mappings in _published_ports().items():
            variable, _ = EXPECTED_BINDINGS[service]
            for mapping in mappings:
                assert resolve(mapping, {}).startswith("127.0.0.1:"), (service, mapping)
                assert resolve(mapping, {variable: "0.0.0.0"}).startswith("0.0.0.0:"), (service, mapping)

    def test_no_service_bypasses_port_publication_with_host_networking(self):
        for name, service in _compose()["services"].items():
            assert service.get("network_mode") != "host", name

    def test_container_still_listens_on_all_interfaces_internally(self):
        compose = _compose()
        environment = compose["services"]["collegue-app"]["environment"]
        dashboard_command = compose["services"]["collegue-dashboard"]["command"]

        assert environment["MCP_HOST"] == "0.0.0.0"
        assert "--server.address=0.0.0.0" in dashboard_command

    def test_publish_host_is_forwarded_to_the_app_for_exposure_warnings(self):
        environment = _compose()["services"]["collegue-app"]["environment"]

        assert environment[PUBLISH_VAR] == "${" + PUBLISH_VAR + ":-127.0.0.1}"

    def test_remote_exposure_with_oauth_is_documented_next_to_the_ports(self):
        text = COMPOSE_PATH.read_text(encoding="utf-8")

        for variable in ("COLLEGUE_PUBLISH_HOST", "COLLEGUE_DASHBOARD_PUBLISH_HOST", "COLLEGUE_KEYCLOAK_PUBLISH_HOST"):
            assert variable in text
        assert "OAUTH_ENABLED=true" in text
        assert "OAUTH_ISSUER" in text

    def test_healthcheck_requires_the_mcp_server_not_only_the_health_server(self):
        """Le health server seul ne suffit pas à déclarer le conteneur sain."""

        healthcheck = _compose()["services"]["collegue-app"]["healthcheck"]
        command = " ".join(str(item) for item in healthcheck["test"])

        assert ":4122/_health" in command
        # Même critère que l'entrypoint (2xx/4xx = à l'écoute), testé dans test_entrypoint_lifecycle.py.
        assert "entrypoint.sh mcp-ready" in command

    @pytest.mark.skipif(
        not _docker_compose_available(),
        reason="docker compose indisponible : la résolution réelle est couverte par le test d'interpolation ci-dessus",
    )
    def test_docker_compose_config_defaults_to_loopback_everywhere(self, tmp_path):
        host_ips = _rendered_host_ips(tmp_path)

        assert host_ips == {
            4121: "127.0.0.1",
            4122: "127.0.0.1",
            4123: "127.0.0.1",
            4125: "127.0.0.1",
            8088: "127.0.0.1",
        }

    @pytest.mark.skipif(not _docker_compose_available(), reason="docker compose indisponible")
    def test_docker_compose_config_remote_exposure_of_the_mcp_surface_keeps_the_rest_local(self, tmp_path):
        host_ips = _rendered_host_ips(tmp_path, COLLEGUE_PUBLISH_HOST="0.0.0.0")

        assert host_ips == {4121: "0.0.0.0", 4122: "0.0.0.0", 8088: "0.0.0.0", 4123: "127.0.0.1", 4125: "127.0.0.1"}

    @pytest.mark.skipif(not _docker_compose_available(), reason="docker compose indisponible")
    @pytest.mark.parametrize(
        ("variable", "port"),
        [("COLLEGUE_DASHBOARD_PUBLISH_HOST", 4125), ("COLLEGUE_KEYCLOAK_PUBLISH_HOST", 4123)],
    )
    def test_docker_compose_config_dashboard_and_keycloak_need_their_own_explicit_opt_in(
        self, tmp_path, variable, port
    ):
        host_ips = _rendered_host_ips(tmp_path, **{variable: "0.0.0.0"})

        assert host_ips[port] == "0.0.0.0"
        assert {p: ip for p, ip in host_ips.items() if p != port} == {
            p: "127.0.0.1" for p in (4121, 4122, 4123, 4125, 8088) if p != port
        }


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
