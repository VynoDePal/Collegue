"""
Best Practices PHP - Ressources pour les bonnes pratiques en PHP moderne
"""
from pydantic import BaseModel
from typing import Dict, List, Optional, Any
import json


class PHPBestPractice(BaseModel):
	"""Modèle pour une bonne pratique PHP."""
	title: str
	description: str
	category: str
	examples: Dict[str, Dict[str, str]] = {}
	references: List[str] = []


PHP_BEST_PRACTICES = {
	"psr12": {
		"title": "Suivre PSR-12 Extended Coding Style",
		"description": "PSR-12 étend PSR-2 et définit le style de codage standard pour PHP. Il couvre l'indentation, les espaces, les déclarations et les structures de contrôle.",
		"category": "style",
		"examples": {
			"good": {
				"title": "Bon style PSR-12",
				"code": "<?php\n\ndeclare(strict_types=1);\n\nnamespace App\\Service;\n\nuse App\\Repository\\UserRepository;\nuse App\\Exception\\UserNotFoundException;\n\nclass UserService\n{\n    public function __construct(\n        private readonly UserRepository $repository\n    ) {}\n\n    public function findOrFail(int $id): User\n    {\n        return $this->repository->find($id)\n            ?? throw new UserNotFoundException($id);\n    }\n}"
			},
			"bad": {
				"title": "Mauvais style",
				"code": "<?php\nnamespace App\\Service;\nuse App\\Repository\\UserRepository;\nclass UserService{\n    private $repository;\n    function __construct($repository){\n        $this->repository=$repository;\n    }\n    function findOrFail($id){\n        $user = $this->repository->find($id);\n        if(!$user) throw new \\Exception('not found');\n        return $user;\n    }\n}"
			}
		},
		"references": ["https://www.php-fig.org/psr/psr-12/"]
	},
	"strict_types": {
		"title": "Activer le typage strict",
		"description": "Utiliser declare(strict_types=1) en haut de chaque fichier PHP pour activer le typage strict et éviter les conversions implicites.",
		"category": "typing",
		"examples": {
			"good": {
				"title": "Avec typage strict",
				"code": "<?php\n\ndeclare(strict_types=1);\n\nfunction add(int $a, int $b): int\n{\n    return $a + $b;\n}\n\n// add('1', '2'); // TypeError!"
			},
			"bad": {
				"title": "Sans typage strict",
				"code": "<?php\n\nfunction add($a, $b)\n{\n    return $a + $b;\n}\n\nadd('1', '2'); // Retourne 3 silencieusement"
			}
		},
		"references": ["https://www.php.net/manual/en/language.types.declarations.php#language.types.declarations.strict"]
	},
	"constructor_promotion": {
		"title": "Utiliser Constructor Property Promotion (PHP 8.0+)",
		"description": "Simplifier les constructeurs en déclarant les propriétés directement dans les paramètres du constructeur.",
		"category": "modern_php",
		"examples": {
			"good": {
				"title": "Avec promotion de constructeur",
				"code": "<?php\n\nclass UserService\n{\n    public function __construct(\n        private readonly UserRepository $repository,\n        private readonly LoggerInterface $logger\n    ) {}\n}"
			},
			"bad": {
				"title": "Style legacy",
				"code": "<?php\n\nclass UserService\n{\n    private UserRepository $repository;\n    private LoggerInterface $logger;\n\n    public function __construct(\n        UserRepository $repository,\n        LoggerInterface $logger\n    ) {\n        $this->repository = $repository;\n        $this->logger = $logger;\n    }\n}"
			}
		},
		"references": ["https://www.php.net/manual/en/language.oop5.decon.php#language.oop5.decon.constructor.promotion"]
	},
	"readonly_properties": {
		"title": "Utiliser readonly pour l'immutabilité (PHP 8.1+)",
		"description": "Marquer les propriétés comme readonly pour garantir qu'elles ne sont assignées qu'une seule fois, améliorant la sécurité et la prédictibilité.",
		"category": "modern_php",
		"examples": {
			"good": {
				"title": "Avec readonly",
				"code": "<?php\n\nclass User\n{\n    public function __construct(\n        public readonly int $id,\n        public readonly string $name,\n        public readonly string $email\n    ) {}\n}"
			},
			"bad": {
				"title": "Sans readonly",
				"code": "<?php\n\nclass User\n{\n    public int $id;\n    public string $name;\n    public string $email;\n\n    // Les propriétés peuvent être modifiées accidentellement\n}"
			}
		},
		"references": ["https://www.php.net/manual/en/language.oop5.properties.php#language.oop5.properties.readonly-properties"]
	},
	"enums": {
		"title": "Utiliser les Enums (PHP 8.1+)",
		"description": "Remplacer les constantes de classe et les strings magiques par des Enums natifs pour un code plus sûr et expressif.",
		"category": "modern_php",
		"examples": {
			"good": {
				"title": "Avec Enum",
				"code": "<?php\n\nenum OrderStatus: string\n{\n    case Pending = 'pending';\n    case Processing = 'processing';\n    case Shipped = 'shipped';\n    case Delivered = 'delivered';\n    case Cancelled = 'cancelled';\n\n    public function label(): string\n    {\n        return match($this) {\n            self::Pending => 'En attente',\n            self::Processing => 'En traitement',\n            self::Shipped => 'Expédié',\n            self::Delivered => 'Livré',\n            self::Cancelled => 'Annulé',\n        };\n    }\n}"
			},
			"bad": {
				"title": "Sans Enum (strings magiques)",
				"code": "<?php\n\nclass Order\n{\n    const STATUS_PENDING = 'pending';\n    const STATUS_PROCESSING = 'processing';\n\n    // Rien n'empêche de passer une valeur invalide\n    public function setStatus(string $status): void\n    {\n        $this->status = $status;\n    }\n}"
			}
		},
		"references": ["https://www.php.net/manual/en/language.enumerations.php"]
	},
	"match_expression": {
		"title": "Utiliser match au lieu de switch (PHP 8.0+)",
		"description": "L'expression match est plus stricte, plus concise et retourne une valeur, contrairement au switch.",
		"category": "modern_php",
		"examples": {
			"good": {
				"title": "Avec match",
				"code": "<?php\n\n$result = match($statusCode) {\n    200 => 'OK',\n    301 => 'Moved Permanently',\n    404 => 'Not Found',\n    500 => 'Internal Server Error',\n    default => 'Unknown',\n};"
			},
			"bad": {
				"title": "Avec switch",
				"code": "<?php\n\nswitch ($statusCode) {\n    case 200:\n        $result = 'OK';\n        break;\n    case 301:\n        $result = 'Moved Permanently';\n        break;\n    case 404:\n        $result = 'Not Found';\n        break;\n    default:\n        $result = 'Unknown';\n        break;\n}"
			}
		},
		"references": ["https://www.php.net/manual/en/control-structures.match.php"]
	},
	"named_arguments": {
		"title": "Utiliser les arguments nommés (PHP 8.0+)",
		"description": "Les arguments nommés améliorent la lisibilité en rendant explicite le rôle de chaque paramètre, surtout pour les fonctions avec plusieurs paramètres optionnels.",
		"category": "modern_php",
		"examples": {
			"good": {
				"title": "Avec arguments nommés",
				"code": "<?php\n\n$user = new User(\n    name: 'John Doe',\n    email: 'john@example.com',\n    isAdmin: false,\n    isActive: true\n);\n\narray_slice(\n    array: $items,\n    offset: 2,\n    length: 5,\n    preserve_keys: true\n);"
			},
			"bad": {
				"title": "Sans arguments nommés",
				"code": "<?php\n\n$user = new User('John Doe', 'john@example.com', false, true);\n// Que signifie false et true ici ?\n\narray_slice($items, 2, 5, true);\n// Que signifie true ici ?"
			}
		},
		"references": ["https://www.php.net/manual/en/functions.arguments.php#functions.named-arguments"]
	},
	"null_safety": {
		"title": "Gérer correctement les null avec les opérateurs modernes",
		"description": "Utiliser le nullsafe operator (?->), null coalescing (??), et les union types pour gérer les valeurs nullables de manière sûre.",
		"category": "error_handling",
		"examples": {
			"good": {
				"title": "Gestion null moderne",
				"code": "<?php\n\n// Nullsafe operator\n$country = $user?->getAddress()?->getCountry()?->getCode();\n\n// Null coalescing\n$name = $request->get('name') ?? 'Anonymous';\n\n// Null coalescing assignment\n$this->cache ??= new Cache();\n\n// Union type nullable\nfunction findUser(int $id): ?User\n{\n    return User::find($id);\n}"
			},
			"bad": {
				"title": "Gestion null legacy",
				"code": "<?php\n\n// Vérifications imbriquées\nif ($user !== null) {\n    $address = $user->getAddress();\n    if ($address !== null) {\n        $country = $address->getCountry();\n        if ($country !== null) {\n            $code = $country->getCode();\n        }\n    }\n}\n\n$name = isset($request->get('name')) ? $request->get('name') : 'Anonymous';"
			}
		},
		"references": ["https://www.php.net/manual/en/language.operators.comparison.php#language.operators.comparison.coalesce"]
	},
	"dependency_injection": {
		"title": "Pratiquer l'injection de dépendances",
		"description": "Injecter les dépendances via le constructeur plutôt que de les instancier à l'intérieur des classes, facilitant les tests et le découplage.",
		"category": "architecture",
		"examples": {
			"good": {
				"title": "Avec injection de dépendance",
				"code": "<?php\n\nclass OrderService\n{\n    public function __construct(\n        private readonly PaymentGatewayInterface $payment,\n        private readonly MailerInterface $mailer,\n        private readonly LoggerInterface $logger\n    ) {}\n\n    public function processOrder(Order $order): void\n    {\n        $this->payment->charge($order->total);\n        $this->mailer->send($order->user->email, 'Order confirmed');\n        $this->logger->info('Order processed', ['id' => $order->id]);\n    }\n}"
			},
			"bad": {
				"title": "Sans injection (couplage fort)",
				"code": "<?php\n\nclass OrderService\n{\n    public function processOrder(Order $order): void\n    {\n        $payment = new StripeGateway();\n        $mailer = new SmtpMailer();\n\n        $payment->charge($order->total);\n        $mailer->send($order->user->email, 'Order confirmed');\n    }\n}"
			}
		},
		"references": ["https://www.php-fig.org/psr/psr-11/"]
	},
	"fiber_async": {
		"title": "Utiliser les Fibers pour les opérations asynchrones (PHP 8.1+)",
		"description": "Les Fibers permettent de suspendre et reprendre l'exécution de code, utile pour les opérations I/O non bloquantes.",
		"category": "modern_php",
		"examples": {
			"good": {
				"title": "Avec Fiber",
				"code": "<?php\n\n$fiber = new Fiber(function (): void {\n    $value = Fiber::suspend('fiber started');\n    echo \"Valeur reçue: $value\\n\";\n});\n\n$result = $fiber->start();\necho $result; // 'fiber started'\n$fiber->resume('hello');"
			},
			"bad": {
				"title": "Appels bloquants séquentiels",
				"code": "<?php\n\n// Chaque appel bloque le suivant\n$users = file_get_contents('https://api.example.com/users');\n$orders = file_get_contents('https://api.example.com/orders');\n$products = file_get_contents('https://api.example.com/products');"
			}
		},
		"references": ["https://www.php.net/manual/en/language.fibers.php"]
	}
}


def get_best_practice(practice_id: str) -> Optional[PHPBestPractice]:
	"""Récupère les informations d'une bonne pratique PHP."""
	if practice_id in PHP_BEST_PRACTICES:
		return PHPBestPractice(**PHP_BEST_PRACTICES[practice_id])
	return None


def get_all_best_practices() -> List[str]:
	"""Récupère la liste de toutes les bonnes pratiques disponibles."""
	return list(PHP_BEST_PRACTICES.keys())


def get_best_practices_by_category(category: str) -> List[str]:
	"""Récupère la liste des bonnes pratiques d'une catégorie spécifique."""
	return [
		id for id, data in PHP_BEST_PRACTICES.items()
		if data.get("category") == category
	]


def register_best_practices(app, app_state):
	"""Enregistre les ressources des bonnes pratiques PHP."""

	@app.resource("collegue://php/best-practices/index")
	def get_best_practices_index() -> str:
		"""Liste toutes les bonnes pratiques PHP disponibles."""
		return json.dumps(get_all_best_practices())

	@app.resource("collegue://php/best-practices/category/{category}")
	def get_best_practices_by_category_resource(category: str) -> str:
		"""Liste les bonnes pratiques d'une catégorie spécifique."""
		return json.dumps(get_best_practices_by_category(category))

	@app.resource("collegue://php/best-practices/{practice_id}")
	def get_best_practice_resource(practice_id: str) -> str:
		"""Récupère les informations d'une bonne pratique spécifique."""
		practice = get_best_practice(practice_id)
		if practice:
			return practice.model_dump_json()
		return json.dumps({"error": f"Bonne pratique {practice_id} non trouvée"})
