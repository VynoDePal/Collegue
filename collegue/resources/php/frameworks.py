"""
Frameworks PHP - Ressources pour les frameworks PHP populaires
"""
from pydantic import BaseModel
from typing import Dict, List, Optional, Any

import json


class PHPFrameworkReference(BaseModel):
	"""Modèle pour une référence de framework PHP."""
	name: str
	description: str
	version: Optional[str] = None
	website: Optional[str] = None
	documentation: Optional[str] = None
	github: Optional[str] = None
	categories: List[str] = []
	features: List[str] = []
	examples: List[Dict[str, str]] = []


PHP_FRAMEWORKS = {

	"laravel": {
		"name": "Laravel",
		"description": "Framework web PHP complet avec syntaxe élégante et outils modernes",
		"version": "11.0",
		"website": "https://laravel.com/",
		"documentation": "https://laravel.com/docs/11.x",
		"github": "https://github.com/laravel/laravel",
		"categories": ["web", "full-stack", "orm", "api"],
		"features": [
			"Eloquent ORM", "Blade Templates", "Artisan CLI",
			"Authentication", "Authorization", "Queue System",
			"Broadcasting", "Task Scheduling", "Testing",
			"Middleware", "Dependency Injection", "Routing"
		],
		"examples": [
			{
				"title": "Route simple",
				"code": "use Illuminate\\Support\\Facades\\Route;\n\nRoute::get('/hello', function () {\n    return response()->json(['message' => 'Hello, World!']);\n});"
			},
			{
				"title": "Contrôleur avec injection de dépendance",
				"code": "<?php\n\nnamespace App\\Http\\Controllers;\n\nuse App\\Services\\UserService;\nuse Illuminate\\Http\\JsonResponse;\n\nclass UserController extends Controller\n{\n    public function __construct(\n        private readonly UserService $userService\n    ) {}\n\n    public function index(): JsonResponse\n    {\n        return response()->json(\n            $this->userService->getAllUsers()\n        );\n    }\n}"
			},
			{
				"title": "Modèle Eloquent",
				"code": "<?php\n\nnamespace App\\Models;\n\nuse Illuminate\\Database\\Eloquent\\Model;\nuse Illuminate\\Database\\Eloquent\\Relations\\HasMany;\n\nclass User extends Model\n{\n    protected $fillable = ['name', 'email'];\n\n    public function orders(): HasMany\n    {\n        return $this->hasMany(Order::class);\n    }\n}"
			}
		]
	},
	"symfony": {
		"name": "Symfony",
		"description": "Framework PHP modulaire et performant pour les applications d'entreprise",
		"version": "7.0",
		"website": "https://symfony.com/",
		"documentation": "https://symfony.com/doc/current/index.html",
		"github": "https://github.com/symfony/symfony",
		"categories": ["web", "enterprise", "api", "modular"],
		"features": [
			"Bundles", "Doctrine ORM", "Twig Templates",
			"Console", "Security", "Forms", "Validator",
			"HTTP Foundation", "Event Dispatcher",
			"Dependency Injection Container", "Routing"
		],
		"examples": [
			{
				"title": "Contrôleur Symfony",
				"code": "<?php\n\nnamespace App\\Controller;\n\nuse Symfony\\Bundle\\FrameworkBundle\\Controller\\AbstractController;\nuse Symfony\\Component\\HttpFoundation\\JsonResponse;\nuse Symfony\\Component\\Routing\\Attribute\\Route;\n\nclass HelloController extends AbstractController\n{\n    #[Route('/hello', methods: ['GET'])]\n    public function index(): JsonResponse\n    {\n        return $this->json(['message' => 'Hello!']);\n    }\n}"
			},
			{
				"title": "Service avec injection",
				"code": "<?php\n\nnamespace App\\Service;\n\nuse Doctrine\\ORM\\EntityManagerInterface;\n\nclass UserService\n{\n    public function __construct(\n        private readonly EntityManagerInterface $em\n    ) {}\n\n    public function findAll(): array\n    {\n        return $this->em->getRepository(User::class)->findAll();\n    }\n}"
			}
		]
	},
	"phpunit": {
		"name": "PHPUnit",
		"description": "Framework de test unitaire standard de facto pour PHP",
		"version": "11.0",
		"website": "https://phpunit.de/",
		"documentation": "https://docs.phpunit.de/en/11.0/",
		"github": "https://github.com/sebastianbergmann/phpunit",
		"categories": ["testing", "unit-testing", "quality-assurance"],
		"features": [
			"Assertions", "Data Providers", "Mock Objects",
			"Code Coverage", "Test Suites", "Fixtures",
			"Annotations/Attributes", "Test Doubles",
			"Database Testing", "XML Configuration"
		],
		"examples": [
			{
				"title": "Test simple",
				"code": "<?php\n\nnamespace Tests\\Unit;\n\nuse PHPUnit\\Framework\\TestCase;\n\nclass CalculatorTest extends TestCase\n{\n    public function test_addition(): void\n    {\n        $calculator = new Calculator();\n        $this->assertEquals(4, $calculator->add(2, 2));\n    }\n\n    public function test_division_by_zero_throws_exception(): void\n    {\n        $this->expectException(\\DivisionByZeroError::class);\n        $calculator = new Calculator();\n        $calculator->divide(10, 0);\n    }\n}"
			},
			{
				"title": "Test avec Data Provider",
				"code": "<?php\n\nuse PHPUnit\\Framework\\TestCase;\nuse PHPUnit\\Framework\\Attributes\\DataProvider;\n\nclass MathTest extends TestCase\n{\n    public static function additionProvider(): array\n    {\n        return [\n            [0, 0, 0],\n            [1, 1, 2],\n            [-1, 1, 0],\n        ];\n    }\n\n    #[DataProvider('additionProvider')]\n    public function test_add(int $a, int $b, int $expected): void\n    {\n        $this->assertEquals($expected, $a + $b);\n    }\n}"
			},
			{
				"title": "Test avec Mock",
				"code": "<?php\n\nuse PHPUnit\\Framework\\TestCase;\n\nclass OrderServiceTest extends TestCase\n{\n    public function test_process_order(): void\n    {\n        $paymentGateway = $this->createMock(PaymentGateway::class);\n        $paymentGateway->expects($this->once())\n            ->method('charge')\n            ->with(100.00)\n            ->willReturn(true);\n\n        $service = new OrderService($paymentGateway);\n        $result = $service->processOrder(100.00);\n\n        $this->assertTrue($result);\n    }\n}"
			}
		]
	},
	"pest": {
		"name": "Pest",
		"description": "Framework de test PHP élégant et moderne, construit sur PHPUnit",
		"version": "3.0",
		"website": "https://pestphp.com/",
		"documentation": "https://pestphp.com/docs/installation",
		"github": "https://github.com/pestphp/pest",
		"categories": ["testing", "unit-testing", "bdd", "quality-assurance"],
		"features": [
			"Syntaxe expressive", "Compatible PHPUnit",
			"Expectations API", "Higher Order Tests",
			"Architectural Testing", "Parallel Testing",
			"Coverage Reports", "Snapshot Testing",
			"Type Coverage", "Plugins"
		],
		"examples": [
			{
				"title": "Test simple avec Pest",
				"code": "<?php\n\ntest('addition works correctly', function () {\n    expect(1 + 1)->toBe(2);\n});\n\ntest('string contains substring', function () {\n    expect('Hello World')->toContain('World');\n});"
			},
			{
				"title": "Test avec expectations chaînées",
				"code": "<?php\n\ntest('user has valid properties', function () {\n    $user = new User('John', 'john@example.com');\n\n    expect($user)\n        ->name->toBe('John')\n        ->email->toBe('john@example.com')\n        ->isActive->toBeTrue();\n});"
			},
			{
				"title": "Test avec beforeEach et datasets",
				"code": "<?php\n\nbeforeEach(function () {\n    $this->calculator = new Calculator();\n});\n\nit('adds two numbers', function (int $a, int $b, int $expected) {\n    expect($this->calculator->add($a, $b))->toBe($expected);\n})->with([\n    [1, 1, 2],\n    [2, 3, 5],\n    [-1, 1, 0],\n]);"
			}
		]
	},
	"codeception": {
		"name": "Codeception",
		"description": "Framework de test tout-en-un pour acceptance, functional et unit testing",
		"version": "5.1",
		"website": "https://codeception.com/",
		"documentation": "https://codeception.com/docs/",
		"github": "https://github.com/Codeception/Codeception",
		"categories": ["testing", "acceptance", "functional", "unit-testing", "bdd"],
		"features": [
			"Tests d'acceptance", "Tests fonctionnels",
			"Tests unitaires", "BDD via Gherkin",
			"Modules Laravel/Symfony", "WebDriver",
			"API Testing", "Database Testing"
		],
		"examples": [
			{
				"title": "Test d'acceptance",
				"code": "<?php\n\nclass LoginCest\n{\n    public function loginSuccessfully(AcceptanceTester $I): void\n    {\n        $I->amOnPage('/login');\n        $I->fillField('email', 'user@example.com');\n        $I->fillField('password', 'secret');\n        $I->click('Login');\n        $I->see('Dashboard');\n    }\n}"
			},
			{
				"title": "Test API",
				"code": "<?php\n\nclass ApiCest\n{\n    public function getUserList(ApiTester $I): void\n    {\n        $I->sendGet('/api/users');\n        $I->seeResponseCodeIs(200);\n        $I->seeResponseIsJson();\n        $I->seeResponseContainsJson([\n            'status' => 'success'\n        ]);\n    }\n}"
			}
		]
	},
	"behat": {
		"name": "Behat",
		"description": "Framework BDD (Behavior-Driven Development) pour PHP avec syntaxe Gherkin",
		"version": "3.14",
		"website": "https://behat.org/",
		"documentation": "https://docs.behat.org/en/latest/",
		"github": "https://github.com/Behat/Behat",
		"categories": ["testing", "bdd", "acceptance"],
		"features": [
			"Syntaxe Gherkin", "Step Definitions",
			"Context Classes", "Hooks",
			"Mink Extension", "Profiles",
			"Tags et filtres", "Formatters"
		],
		"examples": [
			{
				"title": "Feature Gherkin",
				"code": "Feature: User registration\n  In order to access the platform\n  As a visitor\n  I need to be able to register\n\n  Scenario: Successful registration\n    Given I am on the registration page\n    When I fill in \"email\" with \"user@example.com\"\n    And I fill in \"password\" with \"secret123\"\n    And I press \"Register\"\n    Then I should see \"Welcome\""
			},
			{
				"title": "Context avec Step Definitions",
				"code": "<?php\n\nuse Behat\\Behat\\Context\\Context;\n\nclass FeatureContext implements Context\n{\n    private string $output;\n\n    /**\n     * @Given I have a calculator\n     */\n    public function iHaveACalculator(): void\n    {\n        $this->calculator = new Calculator();\n    }\n\n    /**\n     * @When I add :a and :b\n     */\n    public function iAdd(int $a, int $b): void\n    {\n        $this->output = $this->calculator->add($a, $b);\n    }\n\n    /**\n     * @Then the result should be :expected\n     */\n    public function theResultShouldBe(int $expected): void\n    {\n        assert($this->output === $expected);\n    }\n}"
			}
		]
	},
	"phpspec": {
		"name": "PHPSpec",
		"description": "Framework de test orienté spécifications et design pour PHP",
		"version": "7.4",
		"website": "https://phpspec.net/",
		"documentation": "https://phpspec.net/en/stable/",
		"github": "https://github.com/phpspec/phpspec",
		"categories": ["testing", "bdd", "tdd", "specification"],
		"features": [
			"Spec-driven development", "Auto-generation de code",
			"Matchers expressifs", "Prophecy mocking",
			"Code generation", "Formatters"
		],
		"examples": [
			{
				"title": "Spécification simple",
				"code": "<?php\n\nnamespace spec\\App;\n\nuse PhpSpec\\ObjectBehavior;\n\nclass CalculatorSpec extends ObjectBehavior\n{\n    function it_is_initializable()\n    {\n        $this->shouldHaveType(Calculator::class);\n    }\n\n    function it_adds_two_numbers()\n    {\n        $this->add(2, 3)->shouldReturn(5);\n    }\n\n    function it_throws_on_division_by_zero()\n    {\n        $this->shouldThrow(\\InvalidArgumentException::class)\n            ->during('divide', [10, 0]);\n    }\n}"
			}
		]
	},
	"kahlan": {
		"name": "Kahlan",
		"description": "Framework de test BDD moderne avec syntaxe describe-it pour PHP",
		"version": "5.2",
		"website": "https://kahlan.github.io/docs/",
		"documentation": "https://kahlan.github.io/docs/",
		"github": "https://github.com/kahlan/kahlan",
		"categories": ["testing", "bdd", "unit-testing"],
		"features": [
			"Syntaxe describe-it", "Monkey patching",
			"Stubs et mocks intégrés", "Code coverage",
			"Reporters personnalisables", "Auto-loader"
		],
		"examples": [
			{
				"title": "Test BDD avec describe-it",
				"code": "<?php\n\ndescribe('Calculator', function () {\n    beforeEach(function () {\n        $this->calc = new Calculator();\n    });\n\n    it('adds two numbers', function () {\n        expect($this->calc->add(2, 3))->toBe(5);\n    });\n\n    it('throws on division by zero', function () {\n        $closure = function () {\n            $this->calc->divide(10, 0);\n        };\n        expect($closure)->toThrow();\n    });\n});"
			}
		]
	}
}


def get_framework_reference(framework_name: str) -> Optional[PHPFrameworkReference]:
	"""Récupère les informations d'un framework PHP."""
	if framework_name.lower() in PHP_FRAMEWORKS:
		return PHPFrameworkReference(**PHP_FRAMEWORKS[framework_name.lower()])
	return None


def get_all_frameworks() -> List[str]:
	"""Récupère la liste de tous les frameworks disponibles."""
	return list(PHP_FRAMEWORKS.keys())


def get_frameworks_by_category(category: str) -> List[str]:
	"""Récupère la liste des frameworks d'une catégorie spécifique."""
	return [
		name for name, data in PHP_FRAMEWORKS.items()
		if category in data.get("categories", [])
	]


def register_frameworks(app, app_state):
	"""Enregistre les ressources des frameworks PHP."""

	@app.resource("collegue://php/frameworks/index")
	def get_frameworks_index() -> str:
		"""Liste tous les frameworks PHP disponibles."""
		return json.dumps(get_all_frameworks())

	@app.resource("collegue://php/frameworks/category/{category}")
	def get_frameworks_by_category_resource(category: str) -> str:
		"""Liste les frameworks d'une catégorie spécifique."""
		return json.dumps(get_frameworks_by_category(category))

	@app.resource("collegue://php/frameworks/{framework_name}")
	def get_framework_resource(framework_name: str) -> str:
		"""Récupère les informations d'un framework spécifique."""
		framework = get_framework_reference(framework_name)
		if framework:
			return framework.model_dump_json()
		return json.dumps({"error": f"Framework {framework_name} non trouvé"})
