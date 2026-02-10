"""
Kubernetes Scanner for IaC Guardrails.

Scans Kubernetes YAML manifests for security issues.
"""
import re
from typing import List, Dict, Any
from . import BaseScanner, IacFinding


class KubernetesScanner(BaseScanner):
	"""Scanner for Kubernetes manifests."""

	def scan(self, content: str, filepath: str, profile: str) -> List[IacFinding]:
		"""Scan Kubernetes manifest for security issues."""
		findings = []
		lines = content.split('\n')

		try:
			import yaml
			docs = list(yaml.safe_load_all(content))
		except Exception as e:
			self._log_warning(f"Erreur parsing YAML {filepath}: {e}")
			return findings

		for doc_idx, doc in enumerate(docs):
			if not doc or not isinstance(doc, dict):
				continue

			if doc.get('kind') == 'Pod':
				findings.extend(self._scan_pod(doc, filepath, lines, doc_idx))
			elif doc.get('kind') in ['Deployment', 'StatefulSet', 'DaemonSet', 'ReplicaSet']:
				findings.extend(self._scan_deployment(doc, filepath, lines, doc_idx))
			elif doc.get('kind') == 'Service':
				findings.extend(self._scan_service(doc, filepath, lines, doc_idx))

		return findings

	def _scan_pod(self, doc: Dict, filepath: str, lines: List[str], doc_idx: int) -> List[IacFinding]:
		"""Scan Pod spec for security issues."""
		findings = []
		spec = doc.get('spec', {})
		containers = spec.get('containers', [])

		for idx, container in enumerate(containers):
			# Check privileged mode
			if container.get('securityContext', {}).get('privileged', False):
				line = self._find_line(lines, 'privileged:', doc_idx)
				findings.append(IacFinding(
					rule_id='K8S-001',
					rule_title='Container running as privileged',
					severity='critical',
					path=filepath,
					line=line,
					message=f"Container '{container.get('name', idx)}' runs in privileged mode",
					remediation="Set securityContext.privileged: false or remove the field",
					references=['https://kubernetes.io/docs/concepts/security/pod-security-standards/'],
					engine="k8s-scanner"
				))

			# Check resource limits
			if not container.get('resources', {}).get('limits'):
				line = self._find_line(lines, 'containers:', doc_idx)
				findings.append(IacFinding(
					rule_id='K8S-007',
					rule_title='Container without resource limits',
					severity='medium',
					path=filepath,
					line=line,
					message=f"Container '{container.get('name', idx)}' has no resource limits",
					remediation="Define resources.limits.cpu and resources.limits.memory",
					references=['https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/'],
					engine="k8s-scanner"
				))

		return findings

	def _scan_deployment(self, doc: Dict, filepath: str, lines: List[str], doc_idx: int) -> List[IacFinding]:
		"""Scan Deployment spec for security issues."""
		findings = []
		spec = doc.get('spec', {}).get('template', {}).get('spec', {})

		# Check hostNetwork
		if spec.get('hostNetwork', False):
			line = self._find_line(lines, 'hostNetwork:', doc_idx)
			findings.append(IacFinding(
				rule_id='K8S-002',
				rule_title='Host network enabled',
				severity='high',
				path=filepath,
				line=line,
				message="Pod uses hostNetwork which exposes it to network attacks",
				remediation="Remove hostNetwork or set to false",
				references=['https://kubernetes.io/docs/concepts/security/pod-security-standards/'],
				engine="k8s-scanner"
			))

		# Check hostPID
		if spec.get('hostPID', False):
			line = self._find_line(lines, 'hostPID:', doc_idx)
			findings.append(IacFinding(
				rule_id='K8S-003',
				rule_title='Host PID namespace enabled',
				severity='high',
				path=filepath,
				line=line,
				message="Pod shares host PID namespace allowing process visibility/killing",
				remediation="Remove hostPID or set to false",
				references=['https://kubernetes.io/docs/concepts/security/pod-security-standards/'],
				engine="k8s-scanner"
			))

		return findings

	def _scan_service(self, doc: Dict, filepath: str, lines: List[str], doc_idx: int) -> List[IacFinding]:
		"""Scan Service spec for security issues."""
		findings = []
		spec = doc.get('spec', {})

		# Check NodePort on sensitive ports
		if spec.get('type') == 'NodePort':
			for port in spec.get('ports', []):
				node_port = port.get('nodePort', 0)
				if 30000 <= node_port <= 32767:
					line = self._find_line(lines, f"nodePort: {node_port}", doc_idx)
					findings.append(IacFinding(
						rule_id='K8S-009',
						rule_title='NodePort exposes service externally',
						severity='medium',
						path=filepath,
						line=line,
						message=f"Service exposes NodePort {node_port} to external network",
						remediation="Use ClusterIP or LoadBalancer with proper ingress controls",
						references=['https://kubernetes.io/docs/concepts/services-networking/service/'],
						engine="k8s-scanner"
					))

		return findings

	def _find_line(self, lines: List[str], pattern: str, doc_idx: int) -> int:
		"""Find line number containing pattern."""
		for i, line in enumerate(lines, 1):
			if pattern in line:
				return i
		return doc_idx + 1
