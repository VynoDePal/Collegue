#!/usr/bin/env python3
"""
Script de test avancé pour MCP transport http avec gestion de session
"""
import requests
import json
import re
from typing import Optional

class MCPStreamableClient:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.session_id: Optional[str] = None
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"
        }

    def _extract_session_id(self, response_text: str) -> Optional[str]:
        """Extrait l'ID de session de la réponse SSE"""

        session_match = re.search(r'session[_\s]?ID[:\s]+([a-f0-9-]+)', response_text, re.IGNORECASE)
        if session_match:
            return session_match.group(1)


        try:

            response_json = json.loads(response_text)
            if 'result' in response_json and 'sessionId' in response_json['result']:
                return response_json['result']['sessionId']
        except (json.JSONDecodeError, KeyError):
            pass

        return None

    def _get_session_from_logs(self) -> Optional[str]:
        """Récupère l'ID de session depuis les logs Docker"""
        try:
            import subprocess
            result = subprocess.run(
                ['docker', 'compose', 'logs', 'collegue-app', '--tail', '50'],
                capture_output=True, text=True, timeout=10
            )

            if result.returncode == 0:

                matches = re.findall(r'Created new transport with session ID: ([a-f0-9-]+)', result.stdout)
                if matches:
                    return matches[-1]
        except Exception as e:
            print(f"Erreur lors de la récupération des logs: {e}")

        return None

    def initialize(self) -> bool:
        """Initialise la connexion MCP"""
        try:

            init_headers = self.headers.copy()
            init_headers["Accept"] = "application/json, text/event-stream"
            init_headers["Content-Type"] = "application/json"


            response = requests.post(
                self.base_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {
                            "tools": {},
                            "resources": {},
                            "prompts": {}
                        },
                        "clientInfo": {
                            "name": "Test Client",
                            "version": "1.0.0"
                        },
                        "transport": "http"
                    }
                },
                headers=init_headers,
                timeout=10
            )

            print(f"Initialize response: {response.status_code}")
            print(f"Response text: {response.text[:500]}...")


            if 'X-Session-ID' in response.headers:
                self.session_id = response.headers['X-Session-ID']
                print(f"✅ Session ID trouvé dans les headers: {self.session_id}")
            elif 'MCP-Session-ID' in response.headers:
                self.session_id = response.headers['MCP-Session-ID']
                print(f"✅ Session ID trouvé dans les headers: {self.session_id}")
            else:

                self.session_id = self._extract_session_id(response.text)
                if not self.session_id:

                    print("Tentative de récupération de l'ID de session depuis les logs...")
                    self.session_id = self._get_session_from_logs()

            if self.session_id:
                print(f"✅ Session ID trouvé: {self.session_id}")

                self.headers["X-Session-ID"] = self.session_id
                self.headers["MCP-Session-ID"] = self.session_id
            else:
                print("⚠️ Session ID non trouvé, les requêtes suivantes pourraient échouer")

            return response.status_code == 200

        except Exception as e:
            print(f"❌ Erreur lors de l'initialisation: {e}")
            return False

    def list_tools(self) -> bool:
        """Liste les outils disponibles"""
        try:

            url = self.base_url


            request_headers = self.headers.copy()
            if self.session_id:

                request_headers["X-Session-ID"] = self.session_id
                request_headers["MCP-Session-ID"] = self.session_id


                request_json = {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/list",
                    "params": {
                        "sessionId": self.session_id
                    }
                }
            else:
                request_json = {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/list",
                    "params": {}
                }

            response = requests.post(
                url,
                json=request_json,
                headers=request_headers,
                timeout=10
            )

            print(f"Tools list response: {response.status_code}")
            print(f"Response text: {response.text[:500]}...")

            return response.status_code == 200

        except Exception as e:
            print(f"❌ Erreur lors de la liste des outils: {e}")
            return False

    def list_resources(self) -> bool:
        """Liste les ressources disponibles"""
        try:

            url = self.base_url


            request_headers = self.headers.copy()
            if self.session_id:

                request_headers["X-Session-ID"] = self.session_id
                request_headers["MCP-Session-ID"] = self.session_id


                request_json = {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "resources/list",
                    "params": {
                        "sessionId": self.session_id
                    }
                }
            else:
                request_json = {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "resources/list",
                    "params": {}
                }

            response = requests.post(
                url,
                json=request_json,
                headers=request_headers,
                timeout=10
            )

            print(f"Resources list response: {response.status_code}")
            print(f"Response text: {response.text[:500]}...")

            return response.status_code == 200

        except Exception as e:
            print(f"❌ Erreur lors de la liste des ressources: {e}")
            return False

def test_mcp_with_session():
    """Test complet MCP avec gestion de session"""
    print("🚀 Test de connexion MCP avec gestion de session\n")

    client = MCPStreamableClient("http://localhost:8088/mcp/")

    print("1. Test d'initialisation...")
    if not client.initialize():
        print("❌ Échec de l'initialisation")
        return False

    print("\n2. Test de liste des outils...")
    client.list_tools()

    print("\n3. Test de liste des ressources...")
    client.list_resources()

    print("\n✅ Tests terminés!")
    return True

if __name__ == "__main__":
    test_mcp_with_session()
