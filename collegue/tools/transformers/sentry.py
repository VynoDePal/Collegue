"""
Sentry Transformers - Fonctions de transformation des données Sentry.

Transforme les données brutes de l'API Sentry en modèles Pydantic typés.
"""
from typing import Any, Dict, List, TYPE_CHECKING

from ...core.shared import normalize_keys

if TYPE_CHECKING:
	from ..sentry_monitor import (
		ProjectInfo, IssueInfo, EventInfo, ReleaseInfo,
		RepoInfo, TagDistribution, ProjectStats
	)

def transform_projects(projects_data: List[Dict[str, Any]]) -> List['ProjectInfo']:
	from ..sentry_monitor import ProjectInfo
	projects_data = normalize_keys(projects_data) or []
	return [ProjectInfo(
		id=p['id'],
		slug=p['slug'],
		name=p['name'],
		platform=p.get('platform'),
		status=p.get('status', 'active'),
		organization=p.get('organization', {})
	) for p in projects_data]

def transform_project(project_data: Dict[str, Any]) -> 'ProjectInfo':
	from ..sentry_monitor import ProjectInfo
	project_data = normalize_keys(project_data) or {}
	return ProjectInfo(
		id=project_data['id'],
		slug=project_data['slug'],
		name=project_data['name'],
		platform=project_data.get('platform'),
		status=project_data.get('status', 'active'),
		options=project_data.get('options', {}),
		organization=project_data.get('organization', {})
	)

def transform_issues(issues_data: List[Dict[str, Any]], limit: int = 100) -> List['IssueInfo']:
	from ..sentry_monitor import IssueInfo
	issues_data = normalize_keys(issues_data) or []
	return [IssueInfo(
		id=i['id'],
		short_id=i.get('short_id') or i.get('shortid') or '',
		title=i['title'],
		culprit=i.get('culprit'),
		level=i.get('level', 'error'),
		status=i.get('status', 'unresolved'),
		count=i.get('count', 0),
		user_count=i.get('user_count', 0),
		first_seen=i.get('first_seen') or '',
		last_seen=i.get('last_seen') or '',
		permalink=i['permalink'],
		is_unhandled=i.get('is_unhandled', False),
		type=i.get('type', 'error')
	) for i in issues_data[:limit]]

def transform_issue(issue_data: Dict[str, Any]) -> 'IssueInfo':
	from ..sentry_monitor import IssueInfo
	issue_data = normalize_keys(issue_data) or {}
	return IssueInfo(
		id=issue_data['id'],
		short_id=issue_data.get('short_id') or issue_data.get('shortid') or '',
		title=issue_data['title'],
		culprit=issue_data.get('culprit'),
		level=issue_data.get('level', 'error'),
		status=issue_data.get('status', 'unresolved'),
		count=issue_data.get('count', 0),
		user_count=issue_data.get('user_count', 0),
		first_seen=issue_data.get('first_seen') or '',
		last_seen=issue_data.get('last_seen') or '',
		permalink=issue_data['permalink'],
		is_unhandled=issue_data.get('is_unhandled', False),
		type=issue_data.get('type', 'error')
	)

def transform_events(events_data: List[Dict[str, Any]], limit: int = 100) -> List['EventInfo']:
	from ..sentry_monitor import EventInfo
	events_data = normalize_keys(events_data) or []
	events = []
	for e in events_data[:limit]:
		stacktrace = None
		entries = e.get('entries', [])
		for entry in entries:
			if entry.get('type') == 'exception':
				exc_data = entry.get('data', {})
				values = exc_data.get('values', [])
				if values:
					frames = values[0].get('stacktrace', {}).get('frames', [])
					if frames:
						st_lines = []
						for frame in frames[-10:]:
							filename = frame.get('filename', '?')
							lineno = frame.get('line_no', '?')
							func = frame.get('function', '?')
							context = frame.get('context', [])
							st_lines.append(f'  File "{filename}", line {lineno}, in {func}')
							if context:
								for ctx in context[-3:]:
									if isinstance(ctx, list) and len(ctx) >= 2:
										st_lines.append(f'    {ctx[1]}')
						stacktrace = "\n".join(st_lines)
				break

		tags = {}
		for tag in e.get('tags', []):
			if isinstance(tag, dict):
				tags[tag.get('key', '')] = tag.get('value', '')

		user_ctx = e.get('user')
		if user_ctx:
			user_ctx = {
				'id': user_ctx.get('id'),
				'email': user_ctx.get('email'),
				'username': user_ctx.get('username'),
				'ip_address': user_ctx.get('ip_address')
			}

		request_ctx = None
		for entry in entries:
			if entry.get('type') == 'request':
				req_data = entry.get('data', {})
				request_ctx = {
					'url': req_data.get('url'),
					'method': req_data.get('method'),
					'headers': dict(list(req_data.get('headers', []))[:5]) if req_data.get('headers') else None
				}
				break

		events.append(EventInfo(
			event_id=e.get('event_id', ''),
			title=e.get('title', ''),
			message=e.get('message'),
			platform=e.get('platform'),
			timestamp=e.get('date_created', '') or e.get('timestamp', ''),
			tags=tags,
			context=e.get('context', {}),
			stacktrace=stacktrace,
			user=user_ctx,
			request=request_ctx
		))
	return events

def transform_releases(releases_data: List[Dict[str, Any]], limit: int = 100) -> List['ReleaseInfo']:
	from ..sentry_monitor import ReleaseInfo
	releases_data = normalize_keys(releases_data) or []
	return [ReleaseInfo(
		version=r['version'],
		short_version=r.get('short_version', r['version'][:20]),
		date_created=r['date_created'],
		first_event=r.get('first_event'),
		last_event=r.get('last_event'),
		new_groups=r.get('new_groups', 0),
		url=r.get('url')
	) for r in releases_data[:limit]]

def transform_repos(repos_data: List[Dict[str, Any]]) -> List['RepoInfo']:
	from ..sentry_monitor import RepoInfo
	repos_data = normalize_keys(repos_data) or []
	return [RepoInfo(
		id=r['id'],
		name=r['name'],
		provider=r.get('provider', {}).get('id') if isinstance(r.get('provider'), dict) else r.get('provider'),
		url=r.get('url'),
		status=r.get('status', 'active')
	) for r in repos_data]

def transform_tags(tags_data: List[Dict[str, Any]]) -> List['TagDistribution']:
	from ..sentry_monitor import TagDistribution
	tags_data = normalize_keys(tags_data) or []
	return [TagDistribution(
		key=t['key'],
		name=t.get('name', t['key']),
		values=t.get('top_values', [])[:10]
	) for t in tags_data]

def transform_project_stats(stats_data: Dict[str, Any], project: str) -> 'ProjectStats':
	from ..sentry_monitor import ProjectStats
	total_events = stats_data.get('total', 0)
	return ProjectStats(
		project=project,
		total_events=total_events,
		unresolved_issues=stats_data.get('unresolved', 0),
		events_24h=stats_data.get('24h', 0)
	)
