#!/usr/bin/env node

/**
 * Script de démarrage pour Collegue MCP
 *
 * Ce script lance le serveur Collegue MCP avec le transport SSE
 * pour l'intégration avec les clients MCP comme les éditeurs de code
 */

const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');

// Détermine le chemin racine du package
const packageRoot = path.resolve(__dirname, '..');

// Options par défaut
const DEFAULT_HOST = '0.0.0.0';
const DEFAULT_PORT = 4121;
const DEFAULT_TRANSPORT = 'sse'; // Server-Sent Events pour MCP

// Fonction pour vérifier si une commande est disponible
function commandExists(cmd) {
  try {
    const execSync = require('child_process').execSync;
    execSync(`which ${cmd}`, { stdio: 'ignore' });
    return true;
  } catch (e) {
    return false;
  }
}

// Détection de Python
let pythonCommand;
if (commandExists('python') && parseInt(require('child_process').execSync('python -c "import sys; print(sys.version_info[0])"').toString()) >= 3) {
  pythonCommand = 'python';
} else if (commandExists('python3')) {
  pythonCommand = 'python3';
} else {
  console.error('❌ Erreur: Python 3 non trouvé. Veuillez installer Python 3.');
  process.exit(1);
}

// Vérifie les environnements virtuels possibles
let pythonPath;
const venvPaths = [
  path.join(packageRoot, '.venv', process.platform === 'win32' ? 'Scripts' : 'bin', 'python'),
  path.join(packageRoot, 'venv', process.platform === 'win32' ? 'Scripts' : 'bin', 'python')
];

for (const venvPath of venvPaths) {
  if (fs.existsSync(venvPath)) {
    pythonPath = venvPath;
    break;
  }
}

// Utiliser Python par défaut si aucun environnement virtuel n'est trouvé
if (!pythonPath) {
  pythonPath = pythonCommand;
}

// Arguments pour le serveur MCP
const host = process.env.COLLEGUE_HOST || DEFAULT_HOST;
const port = process.env.COLLEGUE_PORT || DEFAULT_PORT;
const transport = DEFAULT_TRANSPORT;

console.log(`🚀 Démarrage de Collegue MCP...`);
console.log(`🐍 Python: ${pythonPath}`);
console.log(`🌐 Hôte: ${host}`);
console.log(`🔌 Port: ${port}`);
console.log(`🔄 Transport: ${transport}`);

// Construction de la commande pour FastMCP
const args = [
  '-m', 'collegue.app',
  '--transport', transport,
  '--host', host,
  '--port', port
];

// Lancement du serveur
const serverProcess = spawn(pythonPath, args, {
  stdio: 'inherit',
  cwd: packageRoot,
  env: { ...process.env }
});

// Gestion des erreurs
serverProcess.on('error', (err) => {
  console.error(`❌ Erreur lors du démarrage du serveur: ${err.message}`);
  process.exit(1);
});

// Gestion des codes d'exit
serverProcess.on('exit', (code) => {
  console.log(`🛑 Serveur MCP arrêté avec le code ${code}`);
  process.exit(code);
});

// Gestion des signaux pour l'arrêt propre
function handleExit() {
  console.log('\n👋 Arrêt du serveur MCP...');
  serverProcess.kill();
}

process.on('SIGINT', handleExit);
process.on('SIGTERM', handleExit);
