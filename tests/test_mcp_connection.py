#!/usr/bin/env python3
"""
Script de test pour vérifier la connexion MCP avec streamable-http

Note importante: D'après le document mcp_configuration_guide.md, il existe un problème connu
avec la transmission de l'ID de session entre le client et le serveur. L'ID de session est 
correctement généré côté serveur lors de l'initialisation, mais la transmission de cet ID
dans les requêtes suivantes ne fonctionne pas correctement. Ce problème est mentionné comme
"Partiellement résolu" et nécessite une correction au niveau du serveur.
"""
import requests
import json
import re
from typing import Optional

def extract_session_id(response_text: str) -> Optional[str]:
    """Extrait l'ID de session de la réponse"""
    # Rechercher l'ID de session dans les logs ou headers
    session_match = re.search(r'session[_\s]?ID[:\s]+([a-f0-9-]+)', response_text, re.IGNORECASE)
    if session_match:
        return session_match.group(1)
    
    # Rechercher l'ID de session au format JSON
    try:
        # Essayer de parser la réponse comme JSON
        for line in response_text.split('\n'):
            if line.startswith('data: '):
                json_data = json.loads(line[6:])
                if 'result' in json_data and 'sessionId' in json_data['result']:
                    return json_data['result']['sessionId']
    except (json.JSONDecodeError, KeyError):
        pass
        
    return None

def test_mcp_connection():
    """Test la connexion au serveur MCP"""
    
    # Test 1: Vérifier que le serveur est accessible
    try:
        response = requests.get("http://localhost:8088/mcp/", timeout=5)
        print(f"✅ Serveur accessible: {response.status_code}")
    except Exception as e:
        print(f"❌ Erreur de connexion: {e}")
        return False
    
    # Test 2: Vérifier l'endpoint de santé via nginx
    try:
        response = requests.get("http://localhost:8088/_health", timeout=5)
        print(f"✅ Health check via nginx: {response.status_code}")
    except Exception as e:
        print(f"❌ Erreur health check nginx: {e}")
    
    # Test 3: Tester l'initialisation MCP avec les bons paramètres
    try:
        # D'abord, créer une session avec tous les paramètres requis
        init_response = requests.post(
            "http://localhost:8088/mcp/",
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
                    "transport": "streamable-http"  # Spécifier explicitement le transport
                }
            },
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream"
            },
            timeout=10
        )
        print(f"✅ Test MCP initialize: {init_response.status_code}")
        print(f"Response: {init_response.text[:300]}...")
        
        # Vérifier s'il y a des cookies dans la réponse
        cookies = init_response.cookies
        if cookies:
            print(f"✅ Cookies trouvés dans la réponse: {dict(cookies)}")
        
        # Extraire l'ID de session
        session_id = None
        if 'X-Session-ID' in init_response.headers:
            session_id = init_response.headers['X-Session-ID']
            print(f"✅ Session ID trouvé dans les headers: {session_id}")
        elif 'MCP-Session-ID' in init_response.headers:
            session_id = init_response.headers['MCP-Session-ID']
            print(f"✅ Session ID trouvé dans les headers: {session_id}")
        else:
            # Essayer d'extraire l'ID de session du corps de la réponse
            session_id = extract_session_id(init_response.text)
            
        if session_id:
            print(f"✅ Session ID trouvé: {session_id}")
            
            # Test des outils disponibles avec l'ID de session
            tools_headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "X-Session-ID": session_id,
                "MCP-Session-ID": session_id,
                "x-session-id": session_id  # Ajouter en minuscules pour nginx
            }
            
            # Inclure l'ID de session dans les paramètres de la requête et dans l'URL
            url_with_session = f"http://localhost:8088/mcp/?session_id={session_id}"
            print(f"Utilisation de l'URL avec session ID: {url_with_session}")
            
            tools_response = requests.post(
                url_with_session,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/list",
                    "params": {
                        "sessionId": session_id
                    }
                },
                headers=tools_headers,
                cookies=cookies,  # Utiliser les cookies de la réponse d'initialisation
                timeout=10
            )
            print(f"✅ Test liste outils: {tools_response.status_code}")
            print(f"Tools Response: {tools_response.text[:300]}...")
        else:
            print("❌ Aucun ID de session trouvé, impossible de continuer les tests")
    except Exception as e:
        print(f"❌ Erreur test MCP: {e}")
    
    print("\n🎯 Configuration recommandée pour Windsurf:")
    print("  URL: http://localhost:8088/mcp/")
    print("  Transport: streamable-http")
    print("  Content-Type: application/json")
    print("  Accept: application/json, text/event-stream")

if __name__ == "__main__":
    test_mcp_connection()
