"""
Kubernetes Transformers - Fonctions de transformation des données Kubernetes.

Transforme les données brutes de l'API Kubernetes en modèles Pydantic typés.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, TYPE_CHECKING

from ...core.shared import normalize_keys

if TYPE_CHECKING:
	from ..kubernetes_ops import (
		PodInfo, PodDetail, ContainerStatus, DeploymentInfo,
		ServiceInfo, NamespaceInfo, EventInfo, NodeInfo,
		ConfigMapInfo, SecretInfo
	)


def _format_age(timestamp: Any) -> str:
	"""Formate un timestamp en âge relatif."""
	if not timestamp:
		return "N/A"

	if hasattr(timestamp, 'timestamp'):
		ts = timestamp
	else:
		return str(timestamp)

	now = datetime.now(timezone.utc)
	delta = now - ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else now - ts

	seconds = int(delta.total_seconds())
	if seconds < 60:
		return f"{seconds}s"
	elif seconds < 3600:
		return f"{seconds // 60}m"
	elif seconds < 86400:
		return f"{seconds // 3600}h"
	else:
		return f"{seconds // 86400}d"


def transform_pods(pods_data: List[Dict[str, Any]]) -> List['PodInfo']:
	"""Transform raw pod data into PodInfo objects."""
	from ..kubernetes_ops import PodInfo
	pods_data = normalize_keys(pods_data) or []
	result = []
	for pod in pods_data:
		metadata = pod.get('metadata', {})
		spec = pod.get('spec', {})
		status = pod.get('status', {})

		# Count restarts and containers
		restarts = 0
		containers = []
		ready_count = 0
		total_count = 0

		container_statuses = status.get('container_statuses', []) or []
		for cs in container_statuses:
			restarts += cs.get('restart_count', 0)
			containers.append(cs.get('name', ''))
			total_count += 1
			if cs.get('ready'):
				ready_count += 1

		result.append(PodInfo(
			name=metadata.get('name', ''),
			namespace=metadata.get('namespace', ''),
			status=status.get('phase', 'Unknown'),
			ready=f"{ready_count}/{total_count}",
			restarts=restarts,
			age=_format_age(metadata.get('creation_timestamp')),
			node=spec.get('node_name'),
			ip=status.get('pod_ip'),
			containers=containers,
			labels=metadata.get('labels', {})
		))
	return result


def transform_pod_detail(pod_data: Dict[str, Any]) -> 'PodDetail':
	"""Transform raw pod data into PodDetail object."""
	from ..kubernetes_ops import PodDetail, ContainerStatus
	pod_data = normalize_keys(pod_data) or {}
	metadata = pod_data.get('metadata', {})
	spec = pod_data.get('spec', {})
	status = pod_data.get('status', {})

	containers = []
	container_statuses = status.get('container_statuses', []) or []
	for cs in container_statuses:
		state = "unknown"
		reason = None
		message = None

		if cs.get('state', {}).get('running'):
			state = "running"
		elif cs.get('state', {}).get('waiting'):
			state = "waiting"
			reason = cs['state']['waiting'].get('reason')
			message = cs['state']['waiting'].get('message')
		elif cs.get('state', {}).get('terminated'):
			state = "terminated"
			reason = cs['state']['terminated'].get('reason')
			message = cs['state']['terminated'].get('message')

		containers.append(ContainerStatus(
			name=cs.get('name', ''),
			ready=cs.get('ready', False),
			restart_count=cs.get('restart_count', 0),
			state=state,
			reason=reason,
			message=message,
			image=cs.get('image', '')
		))

	conditions = []
	for c in status.get('conditions', []):
		conditions.append({
			"type": c.get('type'),
			"status": c.get('status'),
			"reason": c.get('reason'),
			"message": c.get('message')
		})

	annotations = metadata.get('annotations', {}) or {}

	return PodDetail(
		name=metadata.get('name', ''),
		namespace=metadata.get('namespace', ''),
		status=status.get('phase', 'Unknown'),
		phase=status.get('phase', 'Unknown'),
		node=spec.get('node_name'),
		ip=status.get('pod_ip'),
		start_time=status.get('start_time'),
		containers=containers,
		conditions=conditions,
		labels=metadata.get('labels', {}),
		annotations=dict(list(annotations.items())[:10])
	)


def transform_deployments(deployments_data: List[Dict[str, Any]]) -> List['DeploymentInfo']:
	"""Transform raw deployment data into DeploymentInfo objects."""
	from ..kubernetes_ops import DeploymentInfo
	deployments_data = normalize_keys(deployments_data) or []
	result = []
	for dep in deployments_data:
		metadata = dep.get('metadata', {})
		spec = dep.get('spec', {})
		status = dep.get('status', {})

		template_spec = spec.get('template', {}).get('spec', {})
		containers = template_spec.get('containers', [])
		image = containers[0].get('image') if containers else None

		selector = spec.get('selector', {})
		strategy_obj = spec.get('strategy', {})

		result.append(DeploymentInfo(
			name=metadata.get('name', ''),
			namespace=metadata.get('namespace', ''),
			replicas=f"{status.get('ready_replicas', 0)}/{spec.get('replicas', 0)}",
			ready=status.get('ready_replicas', 0),
			available=status.get('available_replicas', 0),
			age=_format_age(metadata.get('creation_timestamp')),
			selector=selector.get('match_labels', {}),
			strategy=strategy_obj.get('type', 'RollingUpdate'),
			image=image
		))
	return result


def transform_deployment(dep_data: Dict[str, Any]) -> 'DeploymentInfo':
	"""Transform raw deployment data into DeploymentInfo object."""
	from ..kubernetes_ops import DeploymentInfo
	dep_data = normalize_keys(dep_data) or {}
	metadata = dep_data.get('metadata', {})
	spec = dep_data.get('spec', {})
	status = dep_data.get('status', {})

	template_spec = spec.get('template', {}).get('spec', {})
	containers = template_spec.get('containers', [])
	image = containers[0].get('image') if containers else None

	selector = spec.get('selector', {})
	strategy_obj = spec.get('strategy', {})

	return DeploymentInfo(
		name=metadata.get('name', ''),
		namespace=metadata.get('namespace', ''),
		replicas=f"{status.get('ready_replicas', 0)}/{spec.get('replicas', 0)}",
		ready=status.get('ready_replicas', 0),
		available=status.get('available_replicas', 0),
		age=_format_age(metadata.get('creation_timestamp')),
		selector=selector.get('match_labels', {}),
		strategy=strategy_obj.get('type', 'RollingUpdate'),
		image=image
	)


def transform_services(services_data: List[Dict[str, Any]]) -> List['ServiceInfo']:
	"""Transform raw service data into ServiceInfo objects."""
	from ..kubernetes_ops import ServiceInfo
	services_data = normalize_keys(services_data) or []
	result = []
	for svc in services_data:
		metadata = svc.get('metadata', {})
		spec = svc.get('spec', {})
		status = svc.get('status', {})

		ports = []
		for p in spec.get('ports', []):
			port_str = f"{p.get('port')}"
			if p.get('node_port'):
				port_str += f":{p['node_port']}"
			port_str += f"/{p.get('protocol', 'TCP')}"
			ports.append(port_str)

		external_ip = None
		lb = status.get('load_balancer', {})
		ingress = lb.get('ingress', [])
		if ingress:
			external_ip = ingress[0].get('ip') or ingress[0].get('hostname')

		result.append(ServiceInfo(
			name=metadata.get('name', ''),
			namespace=metadata.get('namespace', ''),
			type=spec.get('type', 'ClusterIP'),
			cluster_ip=spec.get('cluster_ip'),
			external_ip=external_ip,
			ports=ports,
			selector=spec.get('selector', {}),
			age=_format_age(metadata.get('creation_timestamp'))
		))
	return result


def transform_namespaces(namespaces_data: List[Dict[str, Any]]) -> List['NamespaceInfo']:
	"""Transform raw namespace data into NamespaceInfo objects."""
	from ..kubernetes_ops import NamespaceInfo
	result = []
	for ns in namespaces_data:
		metadata = ns.get('metadata', {})
		status = ns.get('status', {})

		result.append(NamespaceInfo(
			name=metadata.get('name', ''),
			status=status.get('phase', 'Unknown'),
			age=_format_age(metadata.get('creation_timestamp')),
			labels=metadata.get('labels', {})
		))
	return result


def transform_events(events_data: List[Dict[str, Any]]) -> List['EventInfo']:
	"""Transform raw event data into EventInfo objects."""
	from ..kubernetes_ops import EventInfo
	result = []
	for e in events_data:
		metadata = e.get('metadata', {})

		source = e.get('source', {})
		source_str = f"{source.get('component', '')}/{source.get('host', '')}" if source else ""

		involved = e.get('involved_object', {})
		involved_str = f"{involved.get('kind', '')}/{involved.get('name', '')}" if involved else ""

		result.append(EventInfo(
			name=metadata.get('name', ''),
			namespace=metadata.get('namespace', ''),
			type=e.get('type', 'Normal'),
			reason=e.get('reason', ''),
			message=e.get('message', ''),
			source=source_str,
			first_timestamp=str(e.get('first_timestamp')) if e.get('first_timestamp') else None,
			last_timestamp=str(e.get('last_timestamp')) if e.get('last_timestamp') else None,
			count=e.get('count', 1),
			involved_object=involved_str
		))
	return result


def transform_nodes(nodes_data: List[Dict[str, Any]]) -> List['NodeInfo']:
	"""Transform raw node data into NodeInfo objects."""
	from ..kubernetes_ops import NodeInfo
	result = []
	for node in nodes_data:
		metadata = node.get('metadata', {})
		status = node.get('status', {})

		# Extract roles
		roles = []
		for label, value in metadata.get('labels', {}).items():
			if label.startswith("node-role.kubernetes.io/"):
				roles.append(label.split("/")[1])

		# Get status
		node_status = "Unknown"
		for condition in status.get('conditions', []):
			if condition.get('type') == "Ready":
				node_status = "Ready" if condition.get('status') == "True" else "NotReady"
				break

		node_info = status.get('node_info', {})
		capacity = status.get('capacity', {})

		result.append(NodeInfo(
			name=metadata.get('name', ''),
			status=node_status,
			roles=roles or ["<none>"],
			age=_format_age(metadata.get('creation_timestamp')),
			version=node_info.get('kubelet_version', '?'),
			os=node_info.get('os_image', '?'),
			cpu=str(capacity.get('cpu', '?')),
			memory=str(capacity.get('memory', '?')),
			pods=str(capacity.get('pods', '?'))
		))
	return result


def transform_node(node_data: Dict[str, Any]) -> 'NodeInfo':
	"""Transform raw node data into NodeInfo object."""
	from ..kubernetes_ops import NodeInfo
	metadata = node_data.get('metadata', {})
	status = node_data.get('status', {})

	roles = []
	for label, value in metadata.get('labels', {}).items():
		if label.startswith("node-role.kubernetes.io/"):
			roles.append(label.split("/")[1])

	node_status = "Unknown"
	for condition in status.get('conditions', []):
		if condition.get('type') == "Ready":
			node_status = "Ready" if condition.get('status') == "True" else "NotReady"
			break

	node_info = status.get('node_info', {})
	capacity = status.get('capacity', {})

	return NodeInfo(
		name=metadata.get('name', ''),
		status=node_status,
		roles=roles or ["<none>"],
		age=_format_age(metadata.get('creation_timestamp')),
		version=node_info.get('kubelet_version', '?'),
		os=node_info.get('os_image', '?'),
		cpu=str(capacity.get('cpu', '?')),
		memory=str(capacity.get('memory', '?')),
		pods=str(capacity.get('pods', '?'))
	)


def transform_configmaps(configmaps_data: List[Dict[str, Any]]) -> List['ConfigMapInfo']:
	"""Transform raw configmap data into ConfigMapInfo objects."""
	from ..kubernetes_ops import ConfigMapInfo
	result = []
	for cm in configmaps_data:
		metadata = cm.get('metadata', {})
		data = cm.get('data', {}) or {}

		result.append(ConfigMapInfo(
			name=metadata.get('name', ''),
			namespace=metadata.get('namespace', ''),
			data_keys=list(data.keys()),
			age=_format_age(metadata.get('creation_timestamp'))
		))
	return result


def transform_secrets(secrets_data: List[Dict[str, Any]]) -> List['SecretInfo']:
	"""Transform raw secret data into SecretInfo objects."""
	from ..kubernetes_ops import SecretInfo
	result = []
	for s in secrets_data:
		metadata = s.get('metadata', {})
		data = s.get('data', {}) or {}

		result.append(SecretInfo(
			name=metadata.get('name', ''),
			namespace=metadata.get('namespace', ''),
			type=s.get('type', 'Opaque'),
			data_keys=list(data.keys()),
			age=_format_age(metadata.get('creation_timestamp'))
		))
	return result
