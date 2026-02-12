from typing import List, Optional


def validate_in_list(valid_values: List[str], value: str) -> str:
	if value not in valid_values:
		raise ValueError(
			f"Valeur '{value}' invalide. Utilisez: {', '.join(valid_values)}"
		)
	return value


def validate_fast_deep(value: str) -> str:
	return validate_in_list(['fast', 'deep'], value)


def normalize_language(language: str) -> str:
	normalized = language.strip().lower()
	aliases = {
		'js': 'javascript',
		'ts': 'typescript',
		'py': 'python',
		'rb': 'ruby',
		'golang': 'go',
		'c#': 'csharp',
		'c-sharp': 'csharp',
		'csharp': 'csharp',
		'f#': 'fsharp',
		'f-sharp': 'fsharp',
		'html': 'html',
		'htm': 'html',
	}
	return aliases.get(normalized, normalized)


def validate_language(value: str, supported: Optional[List[str]] = None) -> str:
	normalized = normalize_language(value)
	if supported and normalized not in supported:
		raise ValueError(
			f"Langage '{value}' non supporté. Utilisez: {', '.join(supported)}"
		)
	return normalized


def validate_confidence_mode(value: str) -> str:
	return validate_in_list(['conservative', 'balanced', 'aggressive'], value)


def validate_refactoring_type(value: str) -> str:
	return validate_in_list(
		['rename', 'extract', 'simplify', 'optimize', 'clean', 'modernize', 'security'],
		value,
	)


def validate_doc_format(value: str) -> str:
	return validate_in_list(['markdown', 'rst', 'html', 'docstring', 'json'], value)


def validate_doc_style(value: str) -> str:
	return validate_in_list(['standard', 'detailed', 'minimal', 'api'], value)


def validate_test_framework(value: str) -> str:
	return validate_in_list(['pytest', 'jest', 'mocha', 'unittest', 'vitest'], value)


def validate_k8s_command(value: str) -> str:
	valid_commands = [
		'list_pods',
		'get_pod',
		'pod_logs',
		'list_deployments',
		'get_deployment',
		'list_services',
		'list_namespaces',
		'list_nodes',
		'describe_resource',
		'list_configmaps',
		'list_secrets',
	]
	return validate_in_list(valid_commands, value)


def validate_postgres_command(value: str) -> str:
	return validate_in_list(
		['list_schemas', 'list_tables', 'describe_table', 'query'],
		value,
	)


def validate_sentry_command(value: str) -> str:
	return validate_in_list(
		[
			'list_projects',
			'list_issues',
			'get_issue',
			'issue_events',
			'project_stats',
			'list_releases',
		],
		value,
	)


def validate_github_command(value: str) -> str:
	return validate_in_list(
		[
			'list_repos',
			'get_repo',
			'get_file',
			'list_prs',
			'get_pr',
			'create_pr',
			'list_issues',
			'get_issue',
			'create_issue',
			'pr_files',
			'pr_comments',
			'repo_branches',
			'create_branch',
			'update_file',
			'repo_commits',
			'search_code',
			'list_workflows',
			'workflow_runs',
		],
		value,
	)


def create_command_validator(valid_commands: List[str], field_name: str = 'command'):
	def validator(cls, v: str) -> str:
		return validate_in_list(valid_commands, v)
	return validator
