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
LOOPBACK_PORT = re.compile(r"^\$\{" + PUBLISH_VAR + r":-127\.0\.0\.1\}:\d+:\d+$")
_INTERPOLATION = re.compile(r"\$\{(\w+):-([^}]*)\}")


def _published_ports() -> dict[str, list[str]]:
    services = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))["services"]
    return {
        name: [str(p) for p in service.get("ports", [])] for name, service in services.items() if "ports" in service
    }


class TestComposePublishesOnLoopbackByDefault:
    """Profil local : aucun port hôte n'est joignable depuis le réseau sans choix explicite.

    Le conteneur continue d'écouter sur 0.0.0.0 EN INTERNE (entrypoint.sh, MCP_HOST) ; seule la
    PUBLICATION hôte est restreinte, via COLLEGUE_PUBLISH_HOST (défaut 127.0.0.1).
    """

    def test_every_published_port_is_bound_to_the_publish_host_variable(self):
        ports = _published_ports()

        assert set(ports) >= {"collegue-app", "collegue-dashboard", "keycloak", "nginx"}
        for service, mappings in ports.items():
            assert mappings, service
            for mapping in mappings:
                assert LOOPBACK_PORT.match(mapping), (
                    f"{service}: le port '{mapping}' doit être publié via ${{{PUBLISH_VAR}:-127.0.0.1}}:hôte:conteneur"
                )

    def test_default_resolution_is_loopback_and_override_is_possible(self):
        def resolve(mapping: str, env: dict[str, str]) -> str:
            return _INTERPOLATION.sub(lambda m: env.get(m.group(1), m.group(2)), mapping)

        for service, mappings in _published_ports().items():
            for mapping in mappings:
                assert resolve(mapping, {}).startswith("127.0.0.1:"), (service, mapping)
                assert resolve(mapping, {PUBLISH_VAR: "0.0.0.0"}).startswith("0.0.0.0:"), (service, mapping)

    def test_container_still_listens_on_all_interfaces_internally(self):
        compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
        environment = compose["services"]["collegue-app"]["environment"]
        dashboard_command = compose["services"]["collegue-dashboard"]["command"]

        assert environment["MCP_HOST"] == "0.0.0.0"
        assert "--server.address=0.0.0.0" in dashboard_command

    def test_publish_host_is_forwarded_to_the_app_for_exposure_warnings(self):
        compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
        environment = compose["services"]["collegue-app"]["environment"]

        assert environment[PUBLISH_VAR] == "${" + PUBLISH_VAR + ":-127.0.0.1}"

    def test_remote_exposure_is_documented_next_to_the_ports(self):
        text = COMPOSE_PATH.read_text(encoding="utf-8")

        assert PUBLISH_VAR in text
        assert "OAUTH_ENABLED=true" in text

    @pytest.mark.skipif(
        shutil.which("docker") is None
        or subprocess.run(["docker", "compose", "version"], capture_output=True).returncode != 0,
        reason="docker compose indisponible : la résolution réelle est couverte par le test d'interpolation ci-dessus",
    )
    @pytest.mark.parametrize(("publish_host", "expected"), [(None, "127.0.0.1"), ("0.0.0.0", "0.0.0.0")])
    def test_docker_compose_config_resolves_host_ip(self, tmp_path, publish_host, expected):
        shutil.copy(COMPOSE_PATH, tmp_path / "docker-compose.yml")
        (tmp_path / ".env").write_text("", encoding="utf-8")  # env_file requis, contenu vide
        env = {key: value for key, value in os.environ.items() if key != PUBLISH_VAR}
        if publish_host is not None:
            env[PUBLISH_VAR] = publish_host

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
        for name, service in rendered["services"].items():
            for port in service.get("ports", []):
                assert port.get("host_ip") == expected, (name, port)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
