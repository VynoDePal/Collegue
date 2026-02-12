"""
Transformers Package - Fonctions de transformation des données.

Transforme les données brutes des APIs en modèles Pydantic typés.
"""
from .kubernetes import (
	transform_pods,
	transform_pod_detail,
	transform_deployments,
	transform_deployment,
	transform_services,
	transform_namespaces,
	transform_events,
	transform_nodes,
	transform_node,
	transform_configmaps,
	transform_secrets,
	_format_age,
)
from .sentry import (
	transform_projects,
	transform_project,
	transform_issues,
	transform_issue,
	transform_events as transform_sentry_events,
	transform_releases,
	transform_repos as transform_sentry_repos,
	transform_tags,
	transform_project_stats,
)
from .github import (
	transform_repos,
	transform_repo,
	transform_prs,
	transform_pr,
	transform_pr_files,
	transform_pr_comments,
	transform_issues as transform_github_issues,
	transform_issue as transform_github_issue,
	transform_branches,
	transform_branch,
	transform_workflow_runs,
	transform_search_results,
)

__all__ = [
	# Kubernetes
	'transform_pods',
	'transform_pod_detail',
	'transform_deployments',
	'transform_deployment',
	'transform_services',
	'transform_namespaces',
	'transform_events',
	'transform_nodes',
	'transform_node',
	'transform_configmaps',
	'transform_secrets',
	'_format_age',
	# Sentry
	'transform_projects',
	'transform_project',
	'transform_issues',
	'transform_issue',
	'transform_sentry_events',
	'transform_releases',
	'transform_sentry_repos',
	'transform_tags',
	'transform_project_stats',
	# GitHub
	'transform_repos',
	'transform_repo',
	'transform_prs',
	'transform_pr',
	'transform_pr_files',
	'transform_pr_comments',
	'transform_github_issues',
	'transform_github_issue',
	'transform_branches',
	'transform_branch',
	'transform_workflow_runs',
	'transform_search_results',
]
