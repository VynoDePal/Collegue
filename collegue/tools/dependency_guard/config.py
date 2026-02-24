"""
Configuration et constantes pour l'outil Dependency Guard.

Contient les listes de packages malveillants, dépréciés, et les configurations
pour les APIs de registres (PyPI, npm, Packagist).
"""

# Packages connus comme malveillants ou typosquats
KNOWN_MALICIOUS_PACKAGES = {
    'python': [
        'python3-dateutil',
        'jeIlyfish',
        'request',
        'beautifulsoup',
    ],
    'javascript': [
        'crossenv',
        'event-stream',
        'flatmap-stream',
        'eslint-scope',
    ],
    'php': [
        'symfont/process',
        'guzzlehttp/guzzle-http',
        'illuminate/support-helpers',
    ]
}

# Packages dépréciés et leurs remplacements
DEPRECATED_PACKAGES = {
    'python': {
        'pycrypto': 'pycryptodome',
        'PIL': 'pillow',
        'distribute': 'setuptools',
        'nose': 'pytest',
        'mock': 'unittest.mock (built-in)',
    },
    'javascript': {
        'request': 'axios ou node-fetch',
        'moment': 'dayjs ou date-fns',
        'underscore': 'lodash',
        'bower': 'npm ou yarn',
    },
    'php': {
        'mcrypt': 'openssl',
        'mysql': 'mysqli ou pdo',
        'swiftmailer/swiftmailer': 'symfony/mailer',
        'fzaninotto/faker': 'fakerphp/faker',
        'phpunit/phpunit-mock-objects': 'phpunit (built-in)',
    }
}

# URLs des APIs de registres
REGISTRY_URLS = {
    'pypi': "https://pypi.org/pypi/{package}/json",
    'npm': "https://registry.npmjs.org/{package}",
    'packagist': "https://packagist.org/packages/{package}.json"
}

# URLs de l'API OSV (vulnérabilités)
OSV_BATCH_URL = "https://api.osv.dev/v1/querybatch"
OSV_VULN_URL = "https://api.osv.dev/v1/vulns/"

# Mapping langage -> écosystème OSV
LANGUAGE_ECOSYSTEM = {
    'python': 'PyPI',
    'javascript': 'npm',
    'php': 'Packagist'
}

# Taille maximale pour les requêtes batch OSV
OSV_CHUNK_SIZE = 1000
