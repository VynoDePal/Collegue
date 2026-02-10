"""
Kubernetes API client for cluster operations.

Provides a client for common Kubernetes operations with kubectl-like interface.
"""
from typing import Any, Dict, List, Optional

from .base import APIClient, APIResponse, APIError


class KubernetesClient:

    def __init__(
        self,
        kubeconfig: Optional[str] = None,
        context: Optional[str] = None,
        namespace: str = "default",
        timeout: int = 30
    ):
        self.kubeconfig = kubeconfig
        self.context = context
        self.namespace = namespace
        self.timeout = timeout

        # Try to import kubernetes client
        try:
            from kubernetes import client, config
            self._k8s_client = client
            self._k8s_config = config
            self._use_kubectl = False
        except ImportError:
            self._k8s_client = None
            self._k8s_config = None
            self._use_kubectl = True

    def _get_kubectl_args(self) -> List[str]:
        """Build kubectl command arguments."""
        args = ["kubectl"]

        if self.kubeconfig:
            args.extend(["--kubeconfig", self.kubeconfig])

        if self.context:
            args.extend(["--context", self.context])

        args.extend(["--namespace", self.namespace])

        return args

    def _run_kubectl(self, command: List[str]) -> APIResponse:
        """Execute kubectl command."""
        import subprocess

        args = self._get_kubectl_args() + command

        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=self.timeout
            )

            if result.returncode == 0:
                return APIResponse(
                    success=True,
                    data=result.stdout
                )
            else:
                return APIResponse(
                    success=False,
                    error_message=result.stderr
                )

        except subprocess.TimeoutExpired:
            return APIResponse(
                success=False,
                error_message=f"kubectl command timed out after {self.timeout}s"
            )
        except FileNotFoundError:
            return APIResponse(
                success=False,
                error_message="kubectl not found. Please install kubectl."
            )
        except Exception as e:
            return APIResponse(
                success=False,
                error_message=str(e)
            )

    def list_pods(
        self,
        namespace: Optional[str] = None,
        label_selector: Optional[str] = None,
        field_selector: Optional[str] = None
    ) -> APIResponse:
        """List pods in namespace."""
        if self._use_kubectl:
            cmd = ["get", "pods", "-o", "json"]

            ns = namespace or self.namespace
            if ns:
                cmd.extend(["-n", ns])

            if label_selector:
                cmd.extend(["-l", label_selector])

            if field_selector:
                cmd.extend(["--field-selector", field_selector])

            return self._run_kubectl(cmd)
        else:
            # Use Python client
            try:
                self._k8s_config.load_kube_config(
                    config_file=self.kubeconfig,
                    context=self.context
                )
                v1 = self._k8s_client.CoreV1Api()

                ns = namespace or self.namespace
                pods = v1.list_namespaced_pod(
                    namespace=ns,
                    label_selector=label_selector,
                    field_selector=field_selector
                )

                return APIResponse(
                    success=True,
                    data=[pod.to_dict() for pod in pods.items]
                )
            except Exception as e:
                return APIResponse(success=False, error_message=str(e))

    def get_pod(self, name: str, namespace: Optional[str] = None) -> APIResponse:
        """Get details of a specific pod."""
        if self._use_kubectl:
            ns = namespace or self.namespace
            return self._run_kubectl(["get", "pod", name, "-n", ns, "-o", "json"])
        else:
            try:
                self._k8s_config.load_kube_config(
                    config_file=self.kubeconfig,
                    context=self.context
                )
                v1 = self._k8s_client.CoreV1Api()
                ns = namespace or self.namespace
                pod = v1.read_namespaced_pod(name=name, namespace=ns)

                return APIResponse(success=True, data=pod.to_dict())
            except Exception as e:
                return APIResponse(success=False, error_message=str(e))

    def get_pod_logs(
        self,
        name: str,
        namespace: Optional[str] = None,
        container: Optional[str] = None,
        tail_lines: int = 100,
        previous: bool = False
    ) -> APIResponse:
        """Get logs from a pod."""
        if self._use_kubectl:
            cmd = ["logs", name]

            ns = namespace or self.namespace
            if ns:
                cmd.extend(["-n", ns])

            if container:
                cmd.extend(["-c", container])

            cmd.extend(["--tail", str(tail_lines)])

            if previous:
                cmd.append("--previous")

            return self._run_kubectl(cmd)
        else:
            try:
                self._k8s_config.load_kube_config(
                    config_file=self.kubeconfig,
                    context=self.context
                )
                v1 = self._k8s_client.CoreV1Api()
                ns = namespace or self.namespace

                logs = v1.read_namespaced_pod_log(
                    name=name,
                    namespace=ns,
                    container=container,
                    tail_lines=tail_lines,
                    previous=previous
                )

                return APIResponse(success=True, data=logs)
            except Exception as e:
                return APIResponse(success=False, error_message=str(e))

    def list_deployments(self, namespace: Optional[str] = None) -> APIResponse:
        """List deployments in namespace."""
        if self._use_kubectl:
            ns = namespace or self.namespace
            return self._run_kubectl(["get", "deployments", "-n", ns, "-o", "json"])
        else:
            try:
                self._k8s_config.load_kube_config(
                    config_file=self.kubeconfig,
                    context=self.context
                )
                apps_v1 = self._k8s_client.AppsV1Api()
                ns = namespace or self.namespace
                deployments = apps_v1.list_namespaced_deployment(namespace=ns)

                return APIResponse(
                    success=True,
                    data=[d.to_dict() for d in deployments.items]
                )
            except Exception as e:
                return APIResponse(success=False, error_message=str(e))

    def list_services(self, namespace: Optional[str] = None) -> APIResponse:
        """List services in namespace."""
        if self._use_kubectl:
            ns = namespace or self.namespace
            return self._run_kubectl(["get", "services", "-n", ns, "-o", "json"])
        else:
            try:
                self._k8s_config.load_kube_config(
                    config_file=self.kubeconfig,
                    context=self.context
                )
                v1 = self._k8s_client.CoreV1Api()
                ns = namespace or self.namespace
                services = v1.list_namespaced_service(namespace=ns)

                return APIResponse(
                    success=True,
                    data=[s.to_dict() for s in services.items]
                )
            except Exception as e:
                return APIResponse(success=False, error_message=str(e))

    def list_namespaces(self) -> APIResponse:
        """List all namespaces."""
        if self._use_kubectl:
            return self._run_kubectl(["get", "namespaces", "-o", "json"])
        else:
            try:
                self._k8s_config.load_kube_config(
                    config_file=self.kubeconfig,
                    context=self.context
                )
                v1 = self._k8s_client.CoreV1Api()
                namespaces = v1.list_namespace()

                return APIResponse(
                    success=True,
                    data=[ns.to_dict() for ns in namespaces.items]
                )
            except Exception as e:
                return APIResponse(success=False, error_message=str(e))

    def get_deployment(self, name: str, namespace: Optional[str] = None) -> APIResponse:
        """Get details of a specific deployment."""
        if self._use_kubectl:
            ns = namespace or self.namespace
            return self._run_kubectl(["get", "deployment", name, "-n", ns, "-o", "json"])
        else:
            try:
                self._k8s_config.load_kube_config(
                    config_file=self.kubeconfig,
                    context=self.context
                )
                apps_v1 = self._k8s_client.AppsV1Api()
                ns = namespace or self.namespace
                deployment = apps_v1.read_namespaced_deployment(name=name, namespace=ns)

                return APIResponse(success=True, data=deployment.to_dict())
            except Exception as e:
                return APIResponse(success=False, error_message=str(e))

    def list_events(
        self,
        namespace: Optional[str] = None,
        field_selector: Optional[str] = None
    ) -> APIResponse:
        """List events in namespace."""
        if self._use_kubectl:
            cmd = ["get", "events", "-o", "json"]
            ns = namespace or self.namespace
            if ns:
                cmd.extend(["-n", ns])
            if field_selector:
                cmd.extend(["--field-selector", field_selector])
            return self._run_kubectl(cmd)
        else:
            try:
                self._k8s_config.load_kube_config(
                    config_file=self.kubeconfig,
                    context=self.context
                )
                v1 = self._k8s_client.CoreV1Api()
                ns = namespace or self.namespace

                kwargs = {"namespace": ns}
                if field_selector:
                    kwargs["field_selector"] = field_selector

                events = v1.list_namespaced_event(**kwargs)

                # Sort by timestamp (most recent first)
                sorted_events = sorted(
                    events.items,
                    key=lambda e: e.last_timestamp or e.first_timestamp or e.metadata.creation_timestamp,
                    reverse=True
                )

                return APIResponse(
                    success=True,
                    data=[e.to_dict() for e in sorted_events[:50]]
                )
            except Exception as e:
                return APIResponse(success=False, error_message=str(e))

    def list_nodes(self) -> APIResponse:
        """List all nodes in the cluster."""
        if self._use_kubectl:
            return self._run_kubectl(["get", "nodes", "-o", "json"])
        else:
            try:
                self._k8s_config.load_kube_config(
                    config_file=self.kubeconfig,
                    context=self.context
                )
                v1 = self._k8s_client.CoreV1Api()
                nodes = v1.list_node()

                return APIResponse(
                    success=True,
                    data=[n.to_dict() for n in nodes.items]
                )
            except Exception as e:
                return APIResponse(success=False, error_message=str(e))

    def list_configmaps(self, namespace: Optional[str] = None) -> APIResponse:
        """List ConfigMaps in namespace."""
        if self._use_kubectl:
            ns = namespace or self.namespace
            return self._run_kubectl(["get", "configmaps", "-n", ns, "-o", "json"])
        else:
            try:
                self._k8s_config.load_kube_config(
                    config_file=self.kubeconfig,
                    context=self.context
                )
                v1 = self._k8s_client.CoreV1Api()
                ns = namespace or self.namespace
                configmaps = v1.list_namespaced_config_map(namespace=ns)

                return APIResponse(
                    success=True,
                    data=[cm.to_dict() for cm in configmaps.items]
                )
            except Exception as e:
                return APIResponse(success=False, error_message=str(e))

    def list_secrets(self, namespace: Optional[str] = None) -> APIResponse:
        """List Secrets in namespace (metadata only)."""
        if self._use_kubectl:
            ns = namespace or self.namespace
            return self._run_kubectl(["get", "secrets", "-n", ns, "-o", "json"])
        else:
            try:
                self._k8s_config.load_kube_config(
                    config_file=self.kubeconfig,
                    context=self.context
                )
                v1 = self._k8s_client.CoreV1Api()
                ns = namespace or self.namespace
                secrets = v1.list_namespaced_secret(namespace=ns)

                # Redact sensitive data
                data = []
                for s in secrets.items:
                    secret_dict = s.to_dict()
                    if secret_dict.get('data'):
                        secret_dict['data'] = {k: "***REDACTED***" for k in secret_dict['data'].keys()}
                    data.append(secret_dict)

                return APIResponse(success=True, data=data)
            except Exception as e:
                return APIResponse(success=False, error_message=str(e))

    def describe_resource(
        self,
        resource_type: str,
        name: str,
        namespace: Optional[str] = None
    ) -> APIResponse:
        """Describe a resource in YAML format."""
        if self._use_kubectl:
            cmd = ["get", resource_type, name, "-o", "yaml"]
            ns = namespace or self.namespace
            if ns:
                cmd.extend(["-n", ns])
            return self._run_kubectl(cmd)
        else:
            try:
                self._k8s_config.load_kube_config(
                    config_file=self.kubeconfig,
                    context=self.context
                )
                v1 = self._k8s_client.CoreV1Api()
                apps_v1 = self._k8s_client.AppsV1Api()
                ns = namespace or self.namespace

                resource_type = resource_type.lower()

                if resource_type in ('pod', 'pods'):
                    resource = v1.read_namespaced_pod(name=name, namespace=ns)
                elif resource_type in ('deployment', 'deployments', 'deploy'):
                    resource = apps_v1.read_namespaced_deployment(name=name, namespace=ns)
                elif resource_type in ('service', 'services', 'svc'):
                    resource = v1.read_namespaced_service(name=name, namespace=ns)
                elif resource_type in ('configmap', 'configmaps', 'cm'):
                    resource = v1.read_namespaced_config_map(name=name, namespace=ns)
                elif resource_type in ('secret', 'secrets'):
                    resource = v1.read_namespaced_secret(name=name, namespace=ns)
                    if hasattr(resource, 'data') and resource.data:
                        resource.data = {k: "***REDACTED***" for k in resource.data.keys()}
                else:
                    return APIResponse(
                        success=False,
                        error_message=f"Resource type '{resource_type}' not supported"
                    )

                # Convert to dict and return as YAML-like structure
                resource_dict = self._k8s_client.ApiClient().sanitize_for_serialization(resource)
                return APIResponse(success=True, data=resource_dict)
            except Exception as e:
                return APIResponse(success=False, error_message=str(e))
