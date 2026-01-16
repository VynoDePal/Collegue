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
        valid = ['list_pods', 'get_pod', 'pod_logs', 'list_deployments', 'get_deployment',
                 'list_services', 'list_namespaces', 'list_events', 'describe_resource',
                 'list_configmaps', 'list_secrets', 'list_nodes', 'get_node']
        if v not in valid:
            raise ValueError(f"Commande invalide. Valides: {valid}")
        return v


class PodInfo(BaseModel):
    """Information sur un pod."""
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
    """Status d'un container."""
    name: str
    ready: bool
    restart_count: int
    state: str
    reason: Optional[str] = None
    message: Optional[str] = None
    image: str


class PodDetail(BaseModel):
    """Détails d'un pod."""
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
    """Information sur un déploiement."""
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
    """Information sur un service."""
    name: str
    namespace: str
    type: str
    cluster_ip: Optional[str] = None
    external_ip: Optional[str] = None
    ports: List[str] = []
    selector: Dict[str, str] = {}
    age: str


class NamespaceInfo(BaseModel):
    """Information sur un namespace."""
    name: str
    status: str
    age: str
    labels: Dict[str, str] = {}


class EventInfo(BaseModel):
    """Information sur un événement."""
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
    """Information sur un nœud."""
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
    """Information sur un ConfigMap."""
    name: str
    namespace: str
    data_keys: List[str] = []
    age: str


class SecretInfo(BaseModel):
    """Information sur un Secret (sans les données)."""
    name: str
    namespace: str
    type: str
    data_keys: List[str] = []
    age: str


class KubernetesResponse(BaseModel):
    """Modèle de réponse pour les opérations Kubernetes."""
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
    
    def get_name(self) -> str:
        return "kubernetes_ops"
    
    def get_description(self) -> str:
        return "Inspecte les clusters Kubernetes: pods, logs, déploiements, services, événements"
    
    def get_request_model(self) -> Type[BaseModel]:
        return KubernetesRequest
    
    def get_response_model(self) -> Type[BaseModel]:
        return KubernetesResponse
    
    def get_supported_languages(self) -> List[str]:
        return ["yaml"]
    
    def _load_config(self, kubeconfig: Optional[str] = None, context: Optional[str] = None):
        """Charge la configuration Kubernetes."""
        if not HAS_K8S:
            raise ToolExecutionError(
                "kubernetes non installé. Installez avec: pip install kubernetes"
            )
        
        try:
            if kubeconfig:
                config.load_kube_config(config_file=kubeconfig, context=context)
            else:
                # Essayer in-cluster d'abord, puis kubeconfig local
                try:
                    config.load_incluster_config()
                except config.ConfigException:
                    config.load_kube_config(context=context)
        except Exception as e:
            raise ToolExecutionError(f"Erreur de configuration Kubernetes: {e}")
    
    def _format_age(self, timestamp) -> str:
        """Formate un timestamp en âge relatif."""
        if not timestamp:
            return "N/A"
        
        from datetime import datetime, timezone
        
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
    
    def _list_pods(self, namespace: str, label_selector: Optional[str], 
                   field_selector: Optional[str]) -> List[PodInfo]:
        """Liste les pods d'un namespace."""
        v1 = client.CoreV1Api()
        
        kwargs = {"namespace": namespace}
        if label_selector:
            kwargs["label_selector"] = label_selector
        if field_selector:
            kwargs["field_selector"] = field_selector
        
        pods = v1.list_namespaced_pod(**kwargs)
        
        result = []
        for pod in pods.items:
            # Calculer le nombre de restarts
            restarts = 0
            containers = []
            ready_count = 0
            total_count = 0
            
            if pod.status.container_statuses:
                for cs in pod.status.container_statuses:
                    restarts += cs.restart_count
                    containers.append(cs.name)
                    total_count += 1
                    if cs.ready:
                        ready_count += 1
            
            result.append(PodInfo(
                name=pod.metadata.name,
                namespace=pod.metadata.namespace,
                status=pod.status.phase,
                ready=f"{ready_count}/{total_count}",
                restarts=restarts,
                age=self._format_age(pod.metadata.creation_timestamp),
                node=pod.spec.node_name,
                ip=pod.status.pod_ip,
                containers=containers,
                labels=pod.metadata.labels or {}
            ))
        
        return result
    
    def _get_pod(self, name: str, namespace: str) -> PodDetail:
        """Récupère les détails d'un pod."""
        v1 = client.CoreV1Api()
        
        try:
            pod = v1.read_namespaced_pod(name=name, namespace=namespace)
        except ApiException as e:
            if e.status == 404:
                raise ToolExecutionError(f"Pod '{name}' introuvable dans '{namespace}'")
            raise ToolExecutionError(f"Erreur API Kubernetes: {e.reason}")
        
        # Extraire les statuts des containers
        containers = []
        if pod.status.container_statuses:
            for cs in pod.status.container_statuses:
                state = "unknown"
                reason = None
                message = None
                
                if cs.state.running:
                    state = "running"
                elif cs.state.waiting:
                    state = "waiting"
                    reason = cs.state.waiting.reason
                    message = cs.state.waiting.message
                elif cs.state.terminated:
                    state = "terminated"
                    reason = cs.state.terminated.reason
                    message = cs.state.terminated.message
                
                containers.append(ContainerStatus(
                    name=cs.name,
                    ready=cs.ready,
                    restart_count=cs.restart_count,
                    state=state,
                    reason=reason,
                    message=message,
                    image=cs.image
                ))
        
        # Extraire les conditions
        conditions = []
        if pod.status.conditions:
            for c in pod.status.conditions:
                conditions.append({
                    "type": c.type,
                    "status": c.status,
                    "reason": c.reason,
                    "message": c.message
                })
        
        return PodDetail(
            name=pod.metadata.name,
            namespace=pod.metadata.namespace,
            status=pod.status.phase,
            phase=pod.status.phase,
            node=pod.spec.node_name,
            ip=pod.status.pod_ip,
            start_time=str(pod.status.start_time) if pod.status.start_time else None,
            containers=containers,
            conditions=conditions,
            labels=pod.metadata.labels or {},
            annotations=dict(list((pod.metadata.annotations or {}).items())[:10])
        )
    
    def _get_pod_logs(self, name: str, namespace: str, container: Optional[str],
                      tail_lines: int, previous: bool) -> str:
        """Récupère les logs d'un pod."""
        v1 = client.CoreV1Api()
        
        try:
            logs = v1.read_namespaced_pod_log(
                name=name,
                namespace=namespace,
                container=container,
                tail_lines=tail_lines,
                previous=previous
            )
            return logs
        except ApiException as e:
            if e.status == 404:
                raise ToolExecutionError(f"Pod '{name}' introuvable dans '{namespace}'")
            raise ToolExecutionError(f"Erreur récupération logs: {e.reason}")
    
    def _list_deployments(self, namespace: str, label_selector: Optional[str]) -> List[DeploymentInfo]:
        """Liste les déploiements d'un namespace."""
        apps_v1 = client.AppsV1Api()
        
        kwargs = {"namespace": namespace}
        if label_selector:
            kwargs["label_selector"] = label_selector
        
        deployments = apps_v1.list_namespaced_deployment(**kwargs)
        
        result = []
        for dep in deployments.items:
            # Extraire l'image du premier container
            image = None
            if dep.spec.template.spec.containers:
                image = dep.spec.template.spec.containers[0].image
            
            result.append(DeploymentInfo(
                name=dep.metadata.name,
                namespace=dep.metadata.namespace,
                replicas=f"{dep.status.ready_replicas or 0}/{dep.spec.replicas}",
                ready=dep.status.ready_replicas or 0,
                available=dep.status.available_replicas or 0,
                age=self._format_age(dep.metadata.creation_timestamp),
                selector=dep.spec.selector.match_labels or {},
                strategy=dep.spec.strategy.type if dep.spec.strategy else "RollingUpdate",
                image=image
            ))
        
        return result
    
    def _get_deployment(self, name: str, namespace: str) -> DeploymentInfo:
        """Récupère les détails d'un déploiement."""
        apps_v1 = client.AppsV1Api()
        
        try:
            dep = apps_v1.read_namespaced_deployment(name=name, namespace=namespace)
        except ApiException as e:
            if e.status == 404:
                raise ToolExecutionError(f"Deployment '{name}' introuvable dans '{namespace}'")
            raise ToolExecutionError(f"Erreur API Kubernetes: {e.reason}")
        
        image = None
        if dep.spec.template.spec.containers:
            image = dep.spec.template.spec.containers[0].image
        
        return DeploymentInfo(
            name=dep.metadata.name,
            namespace=dep.metadata.namespace,
            replicas=f"{dep.status.ready_replicas or 0}/{dep.spec.replicas}",
            ready=dep.status.ready_replicas or 0,
            available=dep.status.available_replicas or 0,
            age=self._format_age(dep.metadata.creation_timestamp),
            selector=dep.spec.selector.match_labels or {},
            strategy=dep.spec.strategy.type if dep.spec.strategy else "RollingUpdate",
            image=image
        )
    
    def _list_services(self, namespace: str, label_selector: Optional[str]) -> List[ServiceInfo]:
        """Liste les services d'un namespace."""
        v1 = client.CoreV1Api()
        
        kwargs = {"namespace": namespace}
        if label_selector:
            kwargs["label_selector"] = label_selector
        
        services = v1.list_namespaced_service(**kwargs)
        
        result = []
        for svc in services.items:
            ports = []
            if svc.spec.ports:
                for p in svc.spec.ports:
                    port_str = f"{p.port}"
                    if p.node_port:
                        port_str += f":{p.node_port}"
                    port_str += f"/{p.protocol}"
                    ports.append(port_str)
            
            external_ip = None
            if svc.status.load_balancer and svc.status.load_balancer.ingress:
                ingress = svc.status.load_balancer.ingress[0]
                external_ip = ingress.ip or ingress.hostname
            
            result.append(ServiceInfo(
                name=svc.metadata.name,
                namespace=svc.metadata.namespace,
                type=svc.spec.type,
                cluster_ip=svc.spec.cluster_ip,
                external_ip=external_ip,
                ports=ports,
                selector=svc.spec.selector or {},
                age=self._format_age(svc.metadata.creation_timestamp)
            ))
        
        return result
    
    def _list_namespaces(self) -> List[NamespaceInfo]:
        """Liste tous les namespaces."""
        v1 = client.CoreV1Api()
        namespaces = v1.list_namespace()
        
        return [NamespaceInfo(
            name=ns.metadata.name,
            status=ns.status.phase,
            age=self._format_age(ns.metadata.creation_timestamp),
            labels=ns.metadata.labels or {}
        ) for ns in namespaces.items]
    
    def _list_events(self, namespace: str, field_selector: Optional[str]) -> List[EventInfo]:
        """Liste les événements d'un namespace."""
        v1 = client.CoreV1Api()
        
        kwargs = {"namespace": namespace}
        if field_selector:
            kwargs["field_selector"] = field_selector
        
        events = v1.list_namespaced_event(**kwargs)
        
        # Trier par timestamp décroissant
        sorted_events = sorted(
            events.items,
            key=lambda e: e.last_timestamp or e.first_timestamp or e.metadata.creation_timestamp,
            reverse=True
        )
        
        return [EventInfo(
            name=e.metadata.name,
            namespace=e.metadata.namespace,
            type=e.type or "Normal",
            reason=e.reason or "",
            message=e.message or "",
            source=f"{e.source.component or ''}/{e.source.host or ''}" if e.source else "",
            first_timestamp=str(e.first_timestamp) if e.first_timestamp else None,
            last_timestamp=str(e.last_timestamp) if e.last_timestamp else None,
            count=e.count or 1,
            involved_object=f"{e.involved_object.kind}/{e.involved_object.name}" if e.involved_object else ""
        ) for e in sorted_events[:50]]
    
    def _list_nodes(self) -> List[NodeInfo]:
        """Liste tous les nœuds du cluster."""
        v1 = client.CoreV1Api()
        nodes = v1.list_node()
        
        result = []
        for node in nodes.items:
            # Déterminer les rôles
            roles = []
            for label, value in (node.metadata.labels or {}).items():
                if label.startswith("node-role.kubernetes.io/"):
                    roles.append(label.split("/")[1])
            
            # Déterminer le statut
            status = "Unknown"
            for condition in (node.status.conditions or []):
                if condition.type == "Ready":
                    status = "Ready" if condition.status == "True" else "NotReady"
                    break
            
            result.append(NodeInfo(
                name=node.metadata.name,
                status=status,
                roles=roles or ["<none>"],
                age=self._format_age(node.metadata.creation_timestamp),
                version=node.status.node_info.kubelet_version if node.status.node_info else "?",
                os=node.status.node_info.os_image if node.status.node_info else "?",
                cpu=str(node.status.capacity.get("cpu", "?")) if node.status.capacity else "?",
                memory=str(node.status.capacity.get("memory", "?")) if node.status.capacity else "?",
                pods=str(node.status.capacity.get("pods", "?")) if node.status.capacity else "?"
            ))
        
        return result
    
    def _list_configmaps(self, namespace: str) -> List[ConfigMapInfo]:
        """Liste les ConfigMaps d'un namespace."""
        v1 = client.CoreV1Api()
        cms = v1.list_namespaced_config_map(namespace=namespace)
        
        return [ConfigMapInfo(
            name=cm.metadata.name,
            namespace=cm.metadata.namespace,
            data_keys=list((cm.data or {}).keys()),
            age=self._format_age(cm.metadata.creation_timestamp)
        ) for cm in cms.items]
    
    def _list_secrets(self, namespace: str) -> List[SecretInfo]:
        """Liste les Secrets d'un namespace (sans données sensibles)."""
        v1 = client.CoreV1Api()
        secrets = v1.list_namespaced_secret(namespace=namespace)
        
        return [SecretInfo(
            name=s.metadata.name,
            namespace=s.metadata.namespace,
            type=s.type,
            data_keys=list((s.data or {}).keys()),
            age=self._format_age(s.metadata.creation_timestamp)
        ) for s in secrets.items]
    
    def _describe_resource(self, resource_type: str, name: str, namespace: str) -> str:
        """Décrit une ressource en YAML."""
        v1 = client.CoreV1Api()
        apps_v1 = client.AppsV1Api()
        
        resource_type = resource_type.lower()
        
        try:
            if resource_type in ('pod', 'pods'):
                resource = v1.read_namespaced_pod(name=name, namespace=namespace)
            elif resource_type in ('deployment', 'deployments', 'deploy'):
                resource = apps_v1.read_namespaced_deployment(name=name, namespace=namespace)
            elif resource_type in ('service', 'services', 'svc'):
                resource = v1.read_namespaced_service(name=name, namespace=namespace)
            elif resource_type in ('configmap', 'configmaps', 'cm'):
                resource = v1.read_namespaced_config_map(name=name, namespace=namespace)
            elif resource_type in ('secret', 'secrets'):
                resource = v1.read_namespaced_secret(name=name, namespace=namespace)
                # Masquer les données sensibles
                if hasattr(resource, 'data') and resource.data:
                    resource.data = {k: "***REDACTED***" for k in resource.data.keys()}
            else:
                raise ToolExecutionError(f"Type de ressource '{resource_type}' non supporté")
            
            # Convertir en dict et formater en YAML
            resource_dict = client.ApiClient().sanitize_for_serialization(resource)
            
            import yaml
            return yaml.dump(resource_dict, default_flow_style=False, allow_unicode=True)
            
        except ApiException as e:
            if e.status == 404:
                raise ToolExecutionError(f"{resource_type} '{name}' introuvable dans '{namespace}'")
            raise ToolExecutionError(f"Erreur API Kubernetes: {e.reason}")
    
    def _execute_core_logic(self, request: KubernetesRequest, **kwargs) -> KubernetesResponse:
        """Exécute la logique principale."""
        self._load_config(request.kubeconfig, request.context)
        
        if request.command == 'list_pods':
            pods = self._list_pods(request.namespace, request.label_selector, request.field_selector)
            return KubernetesResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(pods)} pod(s) dans '{request.namespace}'",
                pods=pods
            )
        
        elif request.command == 'get_pod':
            if not request.name:
                raise ToolExecutionError("name requis pour get_pod")
            pod = self._get_pod(request.name, request.namespace)
            return KubernetesResponse(
                success=True,
                command=request.command,
                message=f"✅ Pod {pod.name}: {pod.status}",
                pod=pod
            )
        
        elif request.command == 'pod_logs':
            if not request.name:
                raise ToolExecutionError("name requis pour pod_logs")
            logs = self._get_pod_logs(
                request.name, request.namespace, request.container,
                request.tail_lines, request.previous
            )
            return KubernetesResponse(
                success=True,
                command=request.command,
                message=f"✅ Logs de '{request.name}' ({len(logs)} caractères)",
                logs=logs[:50000]  # Limiter la taille
            )
        
        elif request.command == 'list_deployments':
            deployments = self._list_deployments(request.namespace, request.label_selector)
            return KubernetesResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(deployments)} deployment(s) dans '{request.namespace}'",
                deployments=deployments
            )
        
        elif request.command == 'get_deployment':
            if not request.name:
                raise ToolExecutionError("name requis pour get_deployment")
            deployment = self._get_deployment(request.name, request.namespace)
            return KubernetesResponse(
                success=True,
                command=request.command,
                message=f"✅ Deployment {deployment.name}: {deployment.replicas}",
                deployment=deployment
            )
        
        elif request.command == 'list_services':
            services = self._list_services(request.namespace, request.label_selector)
            return KubernetesResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(services)} service(s) dans '{request.namespace}'",
                services=services
            )
        
        elif request.command == 'list_namespaces':
            namespaces = self._list_namespaces()
            return KubernetesResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(namespaces)} namespace(s)",
                namespaces=namespaces
            )
        
        elif request.command == 'list_events':
            events = self._list_events(request.namespace, request.field_selector)
            return KubernetesResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(events)} événement(s) dans '{request.namespace}'",
                events=events
            )
        
        elif request.command == 'list_nodes':
            nodes = self._list_nodes()
            return KubernetesResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(nodes)} nœud(s)",
                nodes=nodes
            )
        
        elif request.command == 'get_node':
            if not request.name:
                raise ToolExecutionError("name requis pour get_node")
            nodes = self._list_nodes()
            node = next((n for n in nodes if n.name == request.name), None)
            if not node:
                raise ToolExecutionError(f"Nœud '{request.name}' introuvable")
            return KubernetesResponse(
                success=True,
                command=request.command,
                message=f"✅ Nœud {node.name}: {node.status}",
                node=node
            )
        
        elif request.command == 'list_configmaps':
            configmaps = self._list_configmaps(request.namespace)
            return KubernetesResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(configmaps)} ConfigMap(s) dans '{request.namespace}'",
                configmaps=configmaps
            )
        
        elif request.command == 'list_secrets':
            secrets = self._list_secrets(request.namespace)
            return KubernetesResponse(
                success=True,
                command=request.command,
                message=f"✅ {len(secrets)} Secret(s) dans '{request.namespace}'",
                secrets=secrets
            )
        
        elif request.command == 'describe_resource':
            if not request.name or not request.resource_type:
                raise ToolExecutionError("name et resource_type requis pour describe_resource")
            yaml_output = self._describe_resource(request.resource_type, request.name, request.namespace)
            return KubernetesResponse(
                success=True,
                command=request.command,
                message=f"✅ Description de {request.resource_type}/{request.name}",
                resource_yaml=yaml_output
            )
        
        else:
            raise ToolExecutionError(f"Commande inconnue: {request.command}")
