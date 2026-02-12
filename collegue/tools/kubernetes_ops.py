"""
Kubernetes Operations Tool - Gestion et inspection des clusters Kubernetes

Permet à Collègue de lister les pods, lire les logs et décrire les ressources.
"""
import logging
import os
import json
from typing import Any, Dict, List, Optional, Type
from pydantic import BaseModel, Field, field_validator
from .base import BaseTool, ToolExecutionError
from .shared import validate_k8s_command
from .clients import KubernetesClient, APIError
from .transformers import (
	transform_pods, transform_pod_detail,
	transform_deployments, transform_deployment,
	transform_services, transform_namespaces,
	transform_events, transform_nodes, transform_node,
	transform_configmaps, transform_secrets,
	_format_age,
)

try:
    from kubernetes import client, config
    from kubernetes.client.rest import ApiException
    HAS_K8S = True
except ImportError:
    HAS_K8S = False


class KubernetesRequest(BaseModel):
    """Modèle de requête pour les opérations Kubernetes.

    PARAMÈTRES REQUIS PAR COMMANDE:
    - list_namespaces, list_nodes: aucun paramètre requis
    - list_pods, list_deployments, list_services, list_events, list_configmaps, list_secrets: namespace (défaut: 'default')
    - get_pod, get_deployment, pod_logs: name + namespace
    - describe_resource: name + resource_type + namespace
    """
    command: str = Field(
        ...,
        description="Commande K8s. get_pod/pod_logs nécessitent 'name'. describe_resource nécessite 'name' ET 'resource_type'. Commandes: list_pods, get_pod, pod_logs, list_deployments, get_deployment, list_services, list_namespaces, list_events, list_nodes, describe_resource, list_configmaps, list_secrets"
    )
    namespace: str = Field("default", description="Namespace Kubernetes (défaut: 'default')")
    name: Optional[str] = Field(
        None,
        description="REQUIS pour get_pod, pod_logs, get_deployment, describe_resource. Nom de la ressource K8s"
    )
    resource_type: Optional[str] = Field(
        None,
        description="REQUIS pour describe_resource. Type: 'pod', 'deployment', 'service', 'configmap', 'secret'"
    )
    container: Optional[str] = Field(None, description="Nom du container spécifique pour pod_logs (optionnel si un seul container)")
    tail_lines: int = Field(100, description="Nombre de lignes de logs à récupérer (1-5000)", ge=1, le=5000)
    previous: bool = Field(False, description="Récupérer les logs du container précédent (après crash)")
    label_selector: Optional[str] = Field(None, description="Filtrer par labels (ex: 'app=nginx', 'env=prod')")
    field_selector: Optional[str] = Field(None, description="Filtrer par champs (ex: 'status.phase=Running')")
    kubeconfig: Optional[str] = Field(None, description="Chemin kubeconfig (utilise ~/.kube/config par défaut)")
    context: Optional[str] = Field(None, description="Contexte K8s à utiliser (optionnel)")

    @field_validator('command')
    @classmethod
    def validate_command(cls, v: str) -> str:
        return validate_k8s_command(v)

class PodInfo(BaseModel):
    name: str
    namespace: str
    status: str
    ready: str
    restarts: int = 0
    age: str
    node: Optional[str] = None
    ip: Optional[str] = None
    containers: List[str] = []
    labels: Dict[str, str] = {}


class ContainerStatus(BaseModel):
    name: str
    ready: bool
    restart_count: int
    state: str
    reason: Optional[str] = None
    message: Optional[str] = None
    image: str


class PodDetail(BaseModel):
    name: str
    namespace: str
    status: str
    phase: str
    node: Optional[str] = None
    ip: Optional[str] = None
    start_time: Optional[str] = None
    containers: List[ContainerStatus] = []
    conditions: List[Dict[str, Any]] = []
    labels: Dict[str, str] = {}
    annotations: Dict[str, str] = {}


class DeploymentInfo(BaseModel):
    name: str
    namespace: str
    replicas: str
    ready: int
    available: int
    age: str
    selector: Dict[str, str] = {}
    strategy: str = "RollingUpdate"
    image: Optional[str] = None


class ServiceInfo(BaseModel):
    name: str
    namespace: str
    type: str
    cluster_ip: Optional[str] = None
    external_ip: Optional[str] = None
    ports: List[str] = []
    selector: Dict[str, str] = {}
    age: str


class NamespaceInfo(BaseModel):
    name: str
    status: str
    age: str
    labels: Dict[str, str] = {}


class EventInfo(BaseModel):
    name: str
    namespace: str
    type: str
    reason: str
    message: str
    source: str
    first_timestamp: Optional[str] = None
    last_timestamp: Optional[str] = None
    count: int = 1
    involved_object: str


class NodeInfo(BaseModel):
    name: str
    status: str
    roles: List[str] = []
    age: str
    version: str
    os: str
    cpu: str
    memory: str
    pods: str


class ConfigMapInfo(BaseModel):
    name: str
    namespace: str
    data_keys: List[str] = []
    age: str


class SecretInfo(BaseModel):
    name: str
    namespace: str
    type: str
    data_keys: List[str] = []
    age: str


class KubernetesResponse(BaseModel):
    success: bool
    command: str
    message: str
    pods: Optional[List[PodInfo]] = None
    pod: Optional[PodDetail] = None
    logs: Optional[str] = None
    deployments: Optional[List[DeploymentInfo]] = None
    deployment: Optional[DeploymentInfo] = None
    services: Optional[List[ServiceInfo]] = None
    namespaces: Optional[List[NamespaceInfo]] = None
    events: Optional[List[EventInfo]] = None
    nodes: Optional[List[NodeInfo]] = None
    node: Optional[NodeInfo] = None
    configmaps: Optional[List[ConfigMapInfo]] = None
    secrets: Optional[List[SecretInfo]] = None
    resource_yaml: Optional[str] = None


class KubernetesOpsTool(BaseTool):
    """
    Outil d'inspection et gestion des clusters Kubernetes.

    Fonctionnalités:
    - Lister et inspecter les pods, déploiements, services
    - Récupérer les logs des containers
    - Voir les événements du cluster
    - Décrire les ressources en YAML
    - Lister les ConfigMaps et Secrets
    """

    tool_name = "kubernetes_ops"
    tool_description = "Inspecte les clusters Kubernetes: pods, logs, déploiements, services, événements"
    tags = {"integration", "devops"}
    request_model = KubernetesRequest
    response_model = KubernetesResponse
    supported_languages = ["yaml"]

    def _load_config(self, kubeconfig: Optional[str] = None, context: Optional[str] = None):
        if not HAS_K8S:
            raise ToolExecutionError(
                "kubernetes non installé. Installez avec: pip install kubernetes"
            )

        try:
            if kubeconfig:
                config.load_kube_config(config_file=kubeconfig, context=context)
            else:

                try:
                    config.load_incluster_config()
                except config.ConfigException:
                    config.load_kube_config(context=context)
        except Exception as e:
            raise ToolExecutionError(f"Erreur de configuration Kubernetes: {e}")

    def _get_kubernetes_client(self, request: KubernetesRequest) -> KubernetesClient:
        return KubernetesClient(
            kubeconfig=request.kubeconfig,
            context=request.context,
            namespace=request.namespace
        )

    def _execute_core_logic(self, request: KubernetesRequest, **kwargs) -> KubernetesResponse:
        client = self._get_kubernetes_client(request)

        if request.command == 'list_pods':
            response = client.list_pods(
                namespace=request.namespace,
                label_selector=request.label_selector,
                field_selector=request.field_selector
            )
            if not response.success:
                raise ToolExecutionError(response.error_message or "Failed to list pods")
            pods_data = response.data or []
            pods = transform_pods(pods_data)
            return KubernetesResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(pods)} pod(s) dans '{request.namespace}'",
                pods=pods
            )

        elif request.command == 'get_pod':
            if not request.name:
                raise ToolExecutionError("name requis pour get_pod")
            response = client.get_pod(request.name, request.namespace)
            if not response.success:
                raise ToolExecutionError(response.error_message or "Failed to get pod")
            pod = transform_pod_detail(response.data)
            return KubernetesResponse(
                success=True,
                command=request.command,
                message=f"✅ Pod {pod.name}: {pod.status}",
                pod=pod
            )

        elif request.command == 'pod_logs':
            if not request.name:
                raise ToolExecutionError("name requis pour pod_logs")
            response = client.get_pod_logs(
                name=request.name,
                namespace=request.namespace,
                container=request.container,
                tail_lines=request.tail_lines,
                previous=request.previous
            )
            if not response.success:
                raise ToolExecutionError(response.error_message or "Failed to get pod logs")
            logs = response.data or ""
            return KubernetesResponse(
                success=True,
                command=request.command,
                message=f"✅ Logs de '{request.name}' ({len(logs)} caractères)",
                logs=logs[:50000]
            )

        elif request.command == 'list_deployments':
            response = client.list_deployments(request.namespace)
            if not response.success:
                raise ToolExecutionError(response.error_message or "Failed to list deployments")
            deployments_data = response.data or []
            deployments = transform_deployments(deployments_data)
            return KubernetesResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(deployments)} deployment(s) dans '{request.namespace}'",
                deployments=deployments
            )

        elif request.command == 'get_deployment':
            if not request.name:
                raise ToolExecutionError("name requis pour get_deployment")
            response = client.get_deployment(request.name, request.namespace)
            if not response.success:
                raise ToolExecutionError(response.error_message or "Failed to get deployment")
            deployment = transform_deployment(response.data)
            return KubernetesResponse(
                success=True,
                command=request.command,
                message=f"✅ Deployment {deployment.name}: {deployment.replicas}",
                deployment=deployment
            )

        elif request.command == 'list_services':
            response = client.list_services(request.namespace)
            if not response.success:
                raise ToolExecutionError(response.error_message or "Failed to list services")
            services_data = response.data or []
            services = transform_services(services_data)
            return KubernetesResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(services)} service(s) dans '{request.namespace}'",
                services=services
            )

        elif request.command == 'list_namespaces':
            response = client.list_namespaces()
            if not response.success:
                raise ToolExecutionError(response.error_message or "Failed to list namespaces")
            namespaces_data = response.data or []
            namespaces = transform_namespaces(namespaces_data)
            return KubernetesResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(namespaces)} namespace(s)",
                namespaces=namespaces
            )

        elif request.command == 'list_events':
            response = client.list_events(
                namespace=request.namespace,
                field_selector=request.field_selector
            )
            if not response.success:
                raise ToolExecutionError(response.error_message or "Failed to list events")
            events_data = response.data or []
            events = transform_events(events_data)
            return KubernetesResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(events)} événement(s) dans '{request.namespace}'",
                events=events
            )

        elif request.command == 'list_nodes':
            response = client.list_nodes()
            if not response.success:
                raise ToolExecutionError(response.error_message or "Failed to list nodes")
            nodes_data = response.data or []
            nodes = transform_nodes(nodes_data)
            return KubernetesResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(nodes)} nœud(s)",
                nodes=nodes
            )

        elif request.command == 'get_node':
            if not request.name:
                raise ToolExecutionError("name requis pour get_node")
            response = client.list_nodes()
            if not response.success:
                raise ToolExecutionError(response.error_message or "Failed to get node")
            nodes_data = response.data or []
            node_dict = next((n for n in nodes_data if n.get('metadata', {}).get('name') == request.name), None)
            if not node_dict:
                raise ToolExecutionError(f"Nœud '{request.name}' introuvable")
            node = transform_node(node_dict)
            return KubernetesResponse(
                success=True,
                command=request.command,
                message=f"✅ Nœud {node.name}: {node.status}",
                node=node
            )

        elif request.command == 'list_configmaps':
            response = client.list_configmaps(request.namespace)
            if not response.success:
                raise ToolExecutionError(response.error_message or "Failed to list configmaps")
            configmaps_data = response.data or []
            configmaps = transform_configmaps(configmaps_data)
            return KubernetesResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(configmaps)} ConfigMap(s) dans '{request.namespace}'",
                configmaps=configmaps
            )

        elif request.command == 'list_secrets':
            response = client.list_secrets(request.namespace)
            if not response.success:
                raise ToolExecutionError(response.error_message or "Failed to list secrets")
            secrets_data = response.data or []
            secrets = transform_secrets(secrets_data)
            return KubernetesResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(secrets)} Secret(s) dans '{request.namespace}'",
                secrets=secrets
            )

        elif request.command == 'describe_resource':
            if not request.name or not request.resource_type:
                raise ToolExecutionError("name et resource_type requis pour describe_resource")
            response = client.describe_resource(
                resource_type=request.resource_type,
                name=request.name,
                namespace=request.namespace
            )
            if not response.success:
                raise ToolExecutionError(response.error_message or "Failed to describe resource")
            import yaml
            yaml_output = yaml.dump(response.data, default_flow_style=False, allow_unicode=True)
            return KubernetesResponse(
                success=True,
                command=request.command,
                message=f"✅ Description de {request.resource_type}/{request.name}",
                resource_yaml=yaml_output
            )

        else:
            raise ToolExecutionError(f"Commande inconnue: {request.command}")
