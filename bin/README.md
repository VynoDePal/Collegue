# MCP Collegue NPX Configuration

Ce dossier contient la configuration pour utiliser le serveur MCP Collegue
via NPX avec un proxy stdio compatible IDE.

## Configuration File

Le fichier `mcp.json` contient la configuration pour utiliser le serveur
Collegue via NPX :

```json
{
  "mcpServers": {
    "collegue": {
      "command": "npx",
      "args": [
        "-y",
        "@collegue/mcp@latest"
      ]
    }
  }
}
```

Cette configuration indique aux outils MCP (Windsurf, Cursor, Claude Desktop,
etc.) d'utiliser le binaire `@collegue/mcp@latest`, qui embarque
le proxy `mcp-remote`.

## How It Works

Quand un IDE compatible MCP doit communiquer avec le serveur Collegue,
il utilise cette configuration pour :

1. Lire le fichier `mcp.json`
2. Extraire la commande et les arguments du serveur `collegue`
3. Executer la commande suivante
4. Communiquer en stdio pendant que `mcp-remote` relaie vers le serveur HTTP

```bash
npx -y @collegue/mcp@latest
```

Le drapeau `-y` repond automatiquement "yes" aux invites d'installation.

## Configuration de l'URL distante

Le binaire lit l'URL distante depuis la variable d'environnement
`MCP_REMOTE_URL`. Si elle n'est pas definie, la valeur par defaut est :
`https://beta.collegue.dev/mcp/`.

## Testing the Configuration

Vous pouvez tester cette configuration avec le script `test_npx_config.js` :

```bash
./test_npx_config.js
```

Le script :
1. Charge la configuration `mcp.json`
2. Valide sa structure
3. Tente d'executer la commande definie

## Comparaison avec la configuration HTTP directe

Cette configuration NPX differe de la connexion HTTP directe utilisee avant :

### Configuration NPX (recommandee pour IDE stdio)
```json
{
  "mcpServers": {
    "collegue": {
      "command": "npx",
      "args": [
        "-y",
        "@collegue/mcp@latest"
      ]
    }
  }
}
```

### Configuration HTTP (ancienne approche)
```json
{
  "collegue": {
    "serverUrl": "https://beta.collegue.dev/mcp/",
    "headers": {
      "Accept": "application/json, text/event-stream",
      "Content-Type": "application/json"
    },
    "transport": "http"
  }
}
```

Les differences cles :
- La configuration NPX reste compatible avec les IDE stdio
- La configuration HTTP directe depend d'un client qui supporte le transport HTTP
- Le proxy `mcp-remote` gere la negociation de transport

## Requirements

Pour utiliser cette configuration, il faut :
- Node.js et NPM installes
- Acces Internet (telechargement de `mcp-remote`)
- Les permissions pour executer NPX