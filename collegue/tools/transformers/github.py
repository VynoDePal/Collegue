"""
GitHub Transformers - Fonctions de transformation des données GitHub.

Transforme les données brutes de l'API GitHub en modèles Pydantic typés.
"""
from typing import Any, Dict, List, TYPE_CHECKING
from ...core.shared import normalize_keys

if TYPE_CHECKING:
	from ..github_commands import (
		RepoInfo, PRInfo, IssueInfo, BranchInfo,
		FileChange, Comment, WorkflowRun, SearchResult
	)


def transform_repos(repos_data: List[Dict[str, Any]], limit: int = 30) -> List['RepoInfo']:
	from ..github_commands import RepoInfo
	repos_data = normalize_keys(repos_data) or []
	return [RepoInfo(
		name=r['name'],
		full_name=r['full_name'],
		description=r.get('description'),
		html_url=r['html_url'],
		default_branch=r.get('default_branch', 'main'),
		language=r.get('language'),
		stars=r.get('stargazers_count', 0),
		forks=r.get('forks_count', 0),
		open_issues=r.get('open_issues_count', 0),
		is_private=r.get('private', False),
		updated_at=r['updated_at']
	) for r in repos_data[:limit]]


def transform_repo(repo_data: Dict[str, Any]) -> 'RepoInfo':
	from ..github_commands import RepoInfo
	repo_data = normalize_keys(repo_data) or {}
	return RepoInfo(
		name=repo_data['name'],
		full_name=repo_data['full_name'],
		description=repo_data.get('description'),
		html_url=repo_data['html_url'],
		default_branch=repo_data.get('default_branch', 'main'),
		language=repo_data.get('language'),
		stars=repo_data.get('stargazers_count', 0),
		forks=repo_data.get('forks_count', 0),
		open_issues=repo_data.get('open_issues_count', 0),
		is_private=repo_data.get('private', False),
		updated_at=repo_data['updated_at']
	)


def transform_prs(prs_data: List[Dict[str, Any]], limit: int = 30) -> List['PRInfo']:
	from ..github_commands import PRInfo
	prs_data = normalize_keys(prs_data) or []
	return [PRInfo(
		number=pr['number'],
		title=pr['title'],
		state=pr['state'],
		html_url=pr['html_url'],
		user=pr['user']['login'],
		base_branch=pr['base']['ref'],
		head_branch=pr['head']['ref'],
		created_at=pr['created_at'],
		updated_at=pr['updated_at'],
		labels=[l['name'] for l in pr.get('labels', [])],
		draft=pr.get('draft', False)
	) for pr in prs_data[:limit]]


def transform_pr(pr_data: Dict[str, Any]) -> 'PRInfo':
	from ..github_commands import PRInfo
	pr_data = normalize_keys(pr_data) or {}
	return PRInfo(
		number=pr_data['number'],
		title=pr_data['title'],
		state=pr_data['state'],
		html_url=pr_data['html_url'],
		user=pr_data['user']['login'],
		base_branch=pr_data['base']['ref'],
		head_branch=pr_data['head']['ref'],
		created_at=pr_data['created_at'],
		updated_at=pr_data['updated_at'],
		labels=[l['name'] for l in pr_data.get('labels', [])],
		draft=pr_data.get('draft', False)
	)


def transform_pr_files(files_data: List[Dict[str, Any]], limit: int = 100) -> List['FileChange']:
	from ..github_commands import FileChange
	files_data = normalize_keys(files_data) or []
	return [FileChange(
		filename=f['filename'],
		status=f['status'],
		additions=f.get('additions', 0),
		deletions=f.get('deletions', 0),
		changes=f.get('changes', 0),
		patch=f.get('patch', '')[:2000] if f.get('patch') else None
	) for f in files_data[:limit]]


def transform_pr_comments(comments_data: List[Dict[str, Any]], limit: int = 100) -> List['Comment']:
	from ..github_commands import Comment
	comments_data = normalize_keys(comments_data) or []
	return [Comment(
		id=c['id'],
		user=c['user']['login'],
		body=c.get('body', ''),
		created_at=c['created_at'],
		path=c.get('path'),
		line=c.get('line')
	) for c in comments_data[:limit]]


def transform_issues(issues_data: List[Dict[str, Any]], limit: int = 30) -> List['IssueInfo']:
	from ..github_commands import IssueInfo
	issues_data = normalize_keys(issues_data) or []
	issues = [i for i in issues_data if 'pull_request' not in i]
	
	return [IssueInfo(
		number=i['number'],
		title=i['title'],
		state=i['state'],
		html_url=i['html_url'],
		user=i['user']['login'],
		labels=[l['name'] for l in i.get('labels', [])],
		assignees=[a['login'] for a in i.get('assignees', [])],
		comments=i.get('comments', 0),
		created_at=i['created_at'],
		updated_at=i['updated_at'],
		body=i.get('body', '')[:500] if i.get('body') else None
	) for i in issues[:limit]]


def transform_issue(issue_data: Dict[str, Any]) -> 'IssueInfo':
	from ..github_commands import IssueInfo
	issue_data = normalize_keys(issue_data) or {}
	return IssueInfo(
		number=issue_data['number'],
		title=issue_data['title'],
		state=issue_data['state'],
		html_url=issue_data['html_url'],
		user=issue_data['user']['login'],
		labels=[l['name'] for l in issue_data.get('labels', [])],
		assignees=[a['login'] for a in issue_data.get('assignees', [])],
		comments=issue_data.get('comments', 0),
		created_at=issue_data['created_at'],
		updated_at=issue_data['updated_at'],
		body=issue_data.get('body', '')[:2000] if issue_data.get('body') else None
	)


def transform_branches(branches_data: List[Dict[str, Any]], limit: int = 100) -> List['BranchInfo']:
	from ..github_commands import BranchInfo
	branches_data = normalize_keys(branches_data) or []
	return [BranchInfo(
		name=b['name'],
		commit_sha=b['commit']['sha'],
		protected=b.get('protected', False)
	) for b in branches_data[:limit]]


def transform_branch(branch_data: Dict[str, Any]) -> 'BranchInfo':
	from ..github_commands import BranchInfo
	branch_data = normalize_keys(branch_data) or {}
	return BranchInfo(
		name=branch_data['name'],
		commit_sha=branch_data['commit']['sha'],
		protected=branch_data.get('protected', False)
	)


def transform_workflow_runs(runs_data: List[Dict[str, Any]], limit: int = 30) -> List['WorkflowRun']:
	from ..github_commands import WorkflowRun
	runs_data = normalize_keys(runs_data) or []
	return [WorkflowRun(
		id=r['id'],
		name=r['name'],
		status=r['status'],
		conclusion=r.get('conclusion'),
		html_url=r['html_url'],
		created_at=r['created_at'],
		head_branch=r['head_branch']
	) for r in runs_data[:limit]]


def transform_search_results(results_data: List[Dict[str, Any]], limit: int = 100) -> List['SearchResult']:
	from ..github_commands import SearchResult
	results_data = normalize_keys(results_data) or []
	return [SearchResult(
		name=r['name'],
		path=r['path'],
		repository=r['repository']['full_name'],
		html_url=r['html_url'],
		score=r.get('score', 0.0)
	) for r in results_data[:limit]]
