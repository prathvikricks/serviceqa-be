"""
Abstract cloud manager and factory for multi-cloud support.
"""
import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class CloudManager(ABC):
    """Base class for cloud service management."""

    @abstractmethod
    def start_service(self, service) -> bool:
        """Start a cloud service. Returns True on success."""
        pass

    @abstractmethod
    def stop_service(self, service) -> bool:
        """Stop a cloud service. Returns True on success."""
        pass

    @abstractmethod
    def get_status(self, service) -> str:
        """Get current status of a cloud service. Returns status string."""
        pass

    @abstractmethod
    def list_resource_groups(self) -> list:
        """
        List resource groups/regions from the cloud provider.
        Returns list of dicts: [{'name': rg_name, 'location': region}, ...]
        """
        pass

    @abstractmethod
    def list_resources(self, service_type: str, resource_group: str = None,
                       region: str = None) -> list:
        """
        Discover available resources of a given type from the cloud provider.
        Returns list of dicts: [{'id': resource_id, 'name': display_name, 'status': status, 'location': region}, ...]
        """
        pass

    def start_services(self, services) -> dict:
        """Start multiple services. Returns {service_id: success_bool}."""
        results = {}
        for service in services:
            try:
                results[service.id] = self.start_service(service)
            except Exception as e:
                logger.error(f"Failed to start service {service.name}: {e}")
                results[service.id] = False
        return results

    def stop_services(self, services) -> dict:
        """Stop multiple services. Returns {service_id: success_bool}."""
        results = {}
        for service in services:
            try:
                results[service.id] = self.stop_service(service)
            except Exception as e:
                logger.error(f"Failed to stop service {service.name}: {e}")
                results[service.id] = False
        return results


class CloudManagerFactory:
    """Creates the appropriate cloud manager based on project provider."""

    _managers = {}

    @classmethod
    def get_manager(cls, project) -> CloudManager:
        """Get or create a cloud manager for a project.

        A project in ``mock`` mode always gets the MockManager, whatever its
        provider — this is the gate that keeps a demo from touching a real
        account. Only ``real`` mode reaches boto3/azure with the decrypted
        credentials.
        """
        mode = getattr(project, 'mode', 'mock') or 'mock'
        cache_key = f"{mode}:{project.cloud_provider}:{project.id}"

        if cache_key not in cls._managers:
            if mode != 'real':
                from .mock_manager import MockManager
                cls._managers[cache_key] = MockManager(
                    project.get_provider_config(), project.cloud_provider)
            elif project.cloud_provider == 'azure':
                from .azure_manager import AzureManager
                cls._managers[cache_key] = AzureManager(project.get_provider_config())
            elif project.cloud_provider == 'aws':
                from .aws_manager import AWSManager
                cls._managers[cache_key] = AWSManager(project.get_provider_config())
            else:
                raise ValueError(f"Unsupported cloud provider: {project.cloud_provider}")

        return cls._managers[cache_key]

    @classmethod
    def clear_cache(cls):
        cls._managers.clear()
