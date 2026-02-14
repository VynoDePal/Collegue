"""
Standard Library PHP - Ressources pour les fonctions et extensions natives PHP
"""
from pydantic import BaseModel
from typing import Dict, List, Optional, Any
import json
import os


class PHPModuleReference(BaseModel):
	"""Modèle pour une référence de module/extension PHP."""
	name: str
	description: str
	version: Optional[str] = None
	functions: List[Dict[str, Any]] = []
	classes: List[Dict[str, Any]] = []
	constants: List[Dict[str, Any]] = []
	examples: List[Dict[str, str]] = []
	url: Optional[str] = None


STDLIB_MODULES = {

	"strings": {
		"name": "Fonctions de chaînes",
		"description": "Fonctions natives PHP pour la manipulation de chaînes de caractères",
		"url": "https://www.php.net/manual/en/ref.strings.php",
		"functions": [
			{"name": "str_contains", "signature": "str_contains(string $haystack, string $needle): bool", "since": "8.0"},
			{"name": "str_starts_with", "signature": "str_starts_with(string $haystack, string $needle): bool", "since": "8.0"},
			{"name": "str_ends_with", "signature": "str_ends_with(string $haystack, string $needle): bool", "since": "8.0"},
			{"name": "mb_str_split", "signature": "mb_str_split(string $string, int $length = 1, ?string $encoding = null): array", "since": "7.4"},
			{"name": "sprintf", "signature": "sprintf(string $format, mixed ...$values): string"},
			{"name": "implode", "signature": "implode(string $separator, array $array): string"},
			{"name": "explode", "signature": "explode(string $separator, string $string, int $limit = PHP_INT_MAX): array"},
			{"name": "trim", "signature": "trim(string $string, string $characters = \" \\n\\r\\t\\v\\x00\"): string"}
		],
		"examples": [
			{"title": "Vérification de contenu", "code": "<?php\n\nif (str_contains($email, '@')) {\n    echo 'Email valide';\n}"},
			{"title": "Manipulation de chaînes", "code": "<?php\n\n$parts = explode(',', 'a,b,c');\n$joined = implode(' | ', $parts);"}
		]
	},
	"arrays": {
		"name": "Fonctions de tableaux",
		"description": "Fonctions natives PHP pour la manipulation de tableaux (arrays)",
		"url": "https://www.php.net/manual/en/ref.array.php",
		"functions": [
			{"name": "array_map", "signature": "array_map(?callable $callback, array $array, array ...$arrays): array"},
			{"name": "array_filter", "signature": "array_filter(array $array, ?callable $callback = null, int $mode = 0): array"},
			{"name": "array_reduce", "signature": "array_reduce(array $array, callable $callback, mixed $initial = null): mixed"},
			{"name": "array_merge", "signature": "array_merge(array ...$arrays): array"},
			{"name": "array_key_exists", "signature": "array_key_exists(string|int $key, array $array): bool"},
			{"name": "in_array", "signature": "in_array(mixed $needle, array $haystack, bool $strict = false): bool"},
			{"name": "array_unique", "signature": "array_unique(array $array, int $flags = SORT_STRING): array"},
			{"name": "array_slice", "signature": "array_slice(array $array, int $offset, ?int $length = null, bool $preserve_keys = false): array"},
			{"name": "array_chunk", "signature": "array_chunk(array $array, int $length, bool $preserve_keys = false): array"},
			{"name": "array_column", "signature": "array_column(array $array, int|string|null $column_key, int|string|null $index_key = null): array"}
		],
		"examples": [
			{"title": "Filtrage et transformation", "code": "<?php\n\n$numbers = [1, 2, 3, 4, 5];\n$even = array_filter($numbers, fn($n) => $n % 2 === 0);\n$doubled = array_map(fn($n) => $n * 2, $numbers);"},
			{"title": "Réduction", "code": "<?php\n\n$total = array_reduce(\n    $items,\n    fn($carry, $item) => $carry + $item['price'],\n    0\n);"}
		]
	},
	"json": {
		"name": "Fonctions JSON",
		"description": "Encodage et décodage JSON natif en PHP",
		"url": "https://www.php.net/manual/en/ref.json.php",
		"functions": [
			{"name": "json_encode", "signature": "json_encode(mixed $value, int $flags = 0, int $depth = 512): string|false"},
			{"name": "json_decode", "signature": "json_decode(string $json, ?bool $associative = null, int $depth = 512, int $flags = 0): mixed"},
			{"name": "json_validate", "signature": "json_validate(string $json, int $depth = 512, int $flags = 0): bool", "since": "8.3"}
		],
		"examples": [
			{"title": "Sérialisation JSON", "code": "<?php\n\n$data = json_encode(\n    ['name' => 'John', 'age' => 30],\n    JSON_PRETTY_PRINT | JSON_THROW_ON_ERROR\n);"},
			{"title": "Désérialisation JSON", "code": "<?php\n\ntry {\n    $obj = json_decode($json, associative: true, flags: JSON_THROW_ON_ERROR);\n} catch (\\JsonException $e) {\n    echo 'JSON invalide: ' . $e->getMessage();\n}"}
		]
	},
	"datetime": {
		"name": "Date et heure",
		"description": "Classes et fonctions pour la gestion des dates et heures en PHP",
		"url": "https://www.php.net/manual/en/book.datetime.php",
		"classes": [
			{"name": "DateTime", "description": "Représentation d'une date/heure mutable"},
			{"name": "DateTimeImmutable", "description": "Représentation d'une date/heure immutable"},
			{"name": "DateInterval", "description": "Représentation d'un intervalle de temps"},
			{"name": "DatePeriod", "description": "Représentation d'une période de dates"}
		],
		"examples": [
			{"title": "DateTimeImmutable", "code": "<?php\n\n$now = new DateTimeImmutable();\n$tomorrow = $now->modify('+1 day');\n$formatted = $now->format('Y-m-d H:i:s');"},
			{"title": "Intervalle et période", "code": "<?php\n\n$start = new DateTimeImmutable('2024-01-01');\n$end = new DateTimeImmutable('2024-12-31');\n$interval = new DateInterval('P1M');\n\nforeach (new DatePeriod($start, $interval, $end) as $date) {\n    echo $date->format('Y-m') . PHP_EOL;\n}"}
		]
	},
	"filesystem": {
		"name": "Système de fichiers",
		"description": "Fonctions pour la manipulation de fichiers et répertoires",
		"url": "https://www.php.net/manual/en/ref.filesystem.php",
		"functions": [
			{"name": "file_get_contents", "signature": "file_get_contents(string $filename, ...): string|false"},
			{"name": "file_put_contents", "signature": "file_put_contents(string $filename, mixed $data, int $flags = 0, ?resource $context = null): int|false"},
			{"name": "is_file", "signature": "is_file(string $filename): bool"},
			{"name": "is_dir", "signature": "is_dir(string $filename): bool"},
			{"name": "glob", "signature": "glob(string $pattern, int $flags = 0): array|false"},
			{"name": "realpath", "signature": "realpath(string $path): string|false"}
		],
		"examples": [
			{"title": "Lecture/écriture de fichier", "code": "<?php\n\n$content = file_get_contents('config.json');\nfile_put_contents('output.json', json_encode($data, JSON_PRETTY_PRINT));"},
			{"title": "Parcours de fichiers", "code": "<?php\n\n$phpFiles = glob('src/**/*.php');\nforeach ($phpFiles as $file) {\n    echo basename($file) . PHP_EOL;\n}"}
		]
	},
	"pdo": {
		"name": "PDO (PHP Data Objects)",
		"description": "Interface d'accès aux bases de données avec requêtes préparées",
		"url": "https://www.php.net/manual/en/book.pdo.php",
		"classes": [
			{"name": "PDO", "description": "Connexion à une base de données"},
			{"name": "PDOStatement", "description": "Requête préparée"},
			{"name": "PDOException", "description": "Exception PDO"}
		],
		"examples": [
			{"title": "Connexion et requête préparée", "code": "<?php\n\n$pdo = new PDO(\n    'mysql:host=localhost;dbname=mydb;charset=utf8mb4',\n    'user',\n    'password',\n    [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]\n);\n\n$stmt = $pdo->prepare('SELECT * FROM users WHERE id = :id');\n$stmt->execute(['id' => 42]);\n$user = $stmt->fetch(PDO::FETCH_ASSOC);"},
			{"title": "Transaction", "code": "<?php\n\ntry {\n    $pdo->beginTransaction();\n    $pdo->exec(\"INSERT INTO orders (user_id, total) VALUES (1, 99.99)\");\n    $pdo->exec(\"UPDATE stock SET quantity = quantity - 1 WHERE product_id = 5\");\n    $pdo->commit();\n} catch (PDOException $e) {\n    $pdo->rollBack();\n    throw $e;\n}"}
		]
	},
	"spl": {
		"name": "SPL (Standard PHP Library)",
		"description": "Collection de classes et interfaces standard pour les structures de données et itérateurs",
		"url": "https://www.php.net/manual/en/book.spl.php",
		"classes": [
			{"name": "SplStack", "description": "Implémentation d'une pile (LIFO)"},
			{"name": "SplQueue", "description": "Implémentation d'une file (FIFO)"},
			{"name": "SplPriorityQueue", "description": "File de priorité"},
			{"name": "SplFixedArray", "description": "Tableau de taille fixe, plus performant"},
			{"name": "SplFileInfo", "description": "Informations sur un fichier"},
			{"name": "ArrayObject", "description": "Tableau accessible comme un objet"}
		],
		"examples": [
			{"title": "Structures de données SPL", "code": "<?php\n\n$stack = new SplStack();\n$stack->push('first');\n$stack->push('second');\necho $stack->pop(); // 'second'\n\n$queue = new SplPriorityQueue();\n$queue->insert('low priority', 1);\n$queue->insert('high priority', 10);\necho $queue->extract(); // 'high priority'"},
			{"title": "Itérateur de répertoire", "code": "<?php\n\n$iterator = new RecursiveIteratorIterator(\n    new RecursiveDirectoryIterator('src/')\n);\n\nforeach ($iterator as $file) {\n    if ($file->isFile() && $file->getExtension() === 'php') {\n        echo $file->getPathname() . PHP_EOL;\n    }\n}"}
		]
	},
	"regex": {
		"name": "Expressions régulières (PCRE)",
		"description": "Fonctions pour les expressions régulières compatibles Perl en PHP",
		"url": "https://www.php.net/manual/en/ref.pcre.php",
		"functions": [
			{"name": "preg_match", "signature": "preg_match(string $pattern, string $subject, ?array &$matches = null, int $flags = 0, int $offset = 0): int|false"},
			{"name": "preg_match_all", "signature": "preg_match_all(string $pattern, string $subject, ?array &$matches = null, int $flags = 0, int $offset = 0): int|false"},
			{"name": "preg_replace", "signature": "preg_replace(string|array $pattern, string|array $replacement, string|array $subject, int $limit = -1, ?int &$count = null): string|array|null"},
			{"name": "preg_split", "signature": "preg_split(string $pattern, string $subject, int $limit = -1, int $flags = 0): array|false"}
		],
		"examples": [
			{"title": "Validation d'email", "code": "<?php\n\n$email = 'user@example.com';\nif (preg_match('/^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}$/', $email)) {\n    echo 'Email valide';\n}"},
			{"title": "Extraction de données", "code": "<?php\n\n$text = 'Prix: 19.99€ et 42.50€';\npreg_match_all('/\\d+\\.\\d{2}/', $text, $matches);\n// $matches[0] = ['19.99', '42.50']"}
		]
	}
}


def get_module_reference(module_name: str) -> Optional[PHPModuleReference]:
	"""Récupère les informations d'un module/extension PHP."""
	if module_name in STDLIB_MODULES:
		return PHPModuleReference(**STDLIB_MODULES[module_name])
	return None


def get_all_modules() -> List[str]:
	"""Récupère la liste de tous les modules disponibles."""
	return list(STDLIB_MODULES.keys())


def register_stdlib(app, app_state):
	"""Enregistre les ressources de la bibliothèque standard PHP."""

	@app.resource("collegue://php/stdlib/index")
	def get_stdlib_index() -> str:
		"""Liste tous les modules de la bibliothèque standard PHP disponibles."""
		return json.dumps(get_all_modules())

	@app.resource("collegue://php/stdlib/{module_name}")
	def get_stdlib_module_resource(module_name: str) -> str:
		"""Récupère les informations d'un module spécifique de la bibliothèque standard."""
		module = get_module_reference(module_name)
		if module:
			return module.model_dump_json()
		return json.dumps({"error": f"Module {module_name} non trouvé"})
