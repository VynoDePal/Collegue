"""
Dockerfile Scanner for IaC Guardrails.

Scans Dockerfiles for security issues.
"""
import re
from typing import List, Dict, Any
from . import BaseScanner, IacFinding


class DockerfileScanner(BaseScanner):
	"""Scanner for Dockerfiles."""

	def scan(self, content: str, filepath: str, profile: str) -> List[IacFinding]:
		"""Scan Dockerfile for security issues."""
		findings = []
		lines = content.split('\n')

		findings.extend(self._scan_from_directive(content, filepath, lines))
		findings.extend(self._scan_user_directive(content, filepath, lines))
		findings.extend(self._scan_secrets(content, filepath, lines))
		findings.extend(self._scan_curl_pipes(content, filepath, lines))
		findings.extend(self._scan_apt_cleanup(content, filepath, lines))
		findings.extend(self._scan_add_vs_copy(content, filepath, lines))

		return findings

	def _scan_from_directive(self, content: str, filepath: str, lines: List[str]) -> List[IacFinding]:
		"""Scan FROM directives for security issues."""
		findings = []

		# Check for latest tag
		pattern = r'^FROM\s+[\w\-./]+:latest'
		for i, line in enumerate(lines, 1):
			if re.match(pattern, line, re.IGNORECASE):
				findings.append(IacFinding(
					rule_id='DOCKER-002',
					rule_title='Using latest tag in FROM',
					severity='medium',
					path=filepath,
					line=i,
					message="Image uses 'latest' tag which is not reproducible",
					remediation="Use a specific version tag (e.g., python:3.11-slim)",
					references=['https://docs.docker.com/develop/develop-images/instructions/#from'],
					engine="docker-scanner"
				))

		# Check for missing tag (defaults to latest)
		pattern = r'^FROM\s+[\w\-./]+\s*$'
		for i, line in enumerate(lines, 1):
			if re.match(pattern, line, re.IGNORECASE):
				findings.append(IacFinding(
					rule_id='DOCKER-003',
					rule_title='No tag specified for base image',
					severity='medium',
					path=filepath,
					line=i,
					message="FROM directive has no tag (defaults to latest)",
					remediation="Specify an explicit version tag",
					references=['https://docs.docker.com/develop/develop-images/instructions/#from'],
					engine="docker-scanner"
				))

		return findings

	def _scan_user_directive(self, content: str, filepath: str, lines: List[str]) -> List[IacFinding]:
		"""Check if container runs as root."""
		findings = []

		# Check if USER directive is present and not root
		has_user = False
		runs_as_root = True

		for i, line in enumerate(lines, 1):
			if re.match(r'^USER\s+', line, re.IGNORECASE):
				has_user = True
				user_match = re.match(r'^USER\s+(\S+)', line, re.IGNORECASE)
				if user_match:
					user = user_match.group(1)
					if user != 'root' and not user.startswith('0'):
						runs_as_root = False

		if not has_user or runs_as_root:
			findings.append(IacFinding(
				rule_id='DOCKER-001',
				rule_title='Container runs as root',
				severity='high',
				path=filepath,
				line=0,
				message="Container runs as root user (no non-root USER directive found)",
				remediation="Add 'USER <non-root-user>' after installing dependencies",
				references=['https://docs.docker.com/develop/develop-images/instructions/#user'],
				engine="docker-scanner"
			))

		return findings

	def _scan_secrets(self, content: str, filepath: str, lines: List[str]) -> List[IacFinding]:
		"""Scan for hardcoded secrets."""
		findings = []

		# Check for hardcoded passwords/keys in ENV or ARG
		pattern = r'(?:ENV|ARG)\s+(?:\w+_)?(?:PASSWORD|SECRET|TOKEN|API_KEY|ACCESS_KEY)\s*=\s*["\']?[^\s"\'$]+["\']?'
		for i, line in enumerate(lines, 1):
			if re.search(pattern, line, re.IGNORECASE):
				findings.append(IacFinding(
					rule_id='DOCKER-005',
					rule_title='Hardcoded secret in Dockerfile',
					severity='critical',
					path=filepath,
					line=i,
					message="Potential secret hardcoded in ENV or ARG directive",
					remediation="Use Docker secrets or environment variables at runtime",
					references=['https://docs.docker.com/engine/swarm/secrets/'],
					engine="docker-scanner"
				))

		return findings

	def _scan_curl_pipes(self, content: str, filepath: str, lines: List[str]) -> List[IacFinding]:
		"""Scan for curl/wget piped to shell."""
		findings = []

		pattern = r'(?:curl|wget)\s+[^\|]+\|\s*(?:bash|sh)'
		for i, line in enumerate(lines, 1):
			if re.search(pattern, line, re.IGNORECASE):
				findings.append(IacFinding(
					rule_id='DOCKER-006',
					rule_title='Curl/wget piped to shell',
					severity='high',
					path=filepath,
					line=i,
					message="Downloading and executing scripts is dangerous (MITM risk)",
					remediation="Download, verify checksum, then execute separately",
					references=['https://blog.aquasec.com/docker-security-best-practices'],
					engine="docker-scanner"
				))

		return findings

	def _scan_apt_cleanup(self, content: str, filepath: str, lines: List[str]) -> List[IacFinding]:
		"""Check for apt-get without cleanup."""
		findings = []

		# Check if apt-get install is present but rm -rf /var/lib/apt is missing
		has_apt_install = False
		has_apt_cleanup = False

		for line in lines:
			if re.search(r'apt-get\s+install', line, re.IGNORECASE):
				has_apt_install = True
			if re.search(r'rm\s+-rf\s+/var/lib/apt', line, re.IGNORECASE):
				has_apt_cleanup = True

		if has_apt_install and not has_apt_cleanup:
			findings.append(IacFinding(
				rule_id='DOCKER-007',
				rule_title='apt-get without cleanup',
				severity='low',
				path=filepath,
				line=0,
				message="apt-get install without cleaning up apt cache increases image size",
				remediation="Add '&& rm -rf /var/lib/apt/lists/*' after apt-get install",
				references=['https://docs.docker.com/develop/develop-images/dockerfile_best-practices/'],
				engine="docker-scanner"
			))

		return findings

	def _scan_add_vs_copy(self, content: str, filepath: str, lines: List[str]) -> List[IacFinding]:
		"""Check for ADD used instead of COPY."""
		findings = []

		pattern = r'^ADD\s+(?!https?://)'
		for i, line in enumerate(lines, 1):
			if re.match(pattern, line, re.IGNORECASE):
				findings.append(IacFinding(
					rule_id='DOCKER-004',
					rule_title='ADD used instead of COPY',
					severity='low',
					path=filepath,
					line=i,
					message="ADD has auto-extract behavior that can be dangerous",
					remediation="Use COPY for local files instead of ADD",
					references=['https://docs.docker.com/develop/develop-images/instructions/#add-or-copy'],
					engine="docker-scanner"
				))

		return findings
