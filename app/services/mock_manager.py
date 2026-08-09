"""Simulated cloud manager.

Every project starts in ``mock`` mode, so the whole scheduling/approval flow is
demoable with no cloud credentials and no spend. Start/stop just flip the stored
``current_status`` on the CloudService row; discovery returns a small fixed
catalogue shaped exactly like the real managers' output, so the admin resource
picker works identically in both modes.

The one rule this file exists to enforce: a mock project must never reach a real
cloud account. Nothing here imports boto3 or azure.
"""
import logging

from .cloud_manager import CloudManager

logger = logging.getLogger(__name__)

# Fake inventory per service type — ids look like the real thing so a project
# switched from mock to real doesn't surprise anyone with obviously-fake data.
_CATALOGUE = {
    'ec2': [
        ('i-0mock1111aaaa2222', 'mock-web-01', 't3.micro'),
        ('i-0mock3333bbbb4444', 'mock-api-01', 't3.small'),
    ],
    'rds': [('mock-postgres-01', 'mock-postgres-01', 'db.t3.micro')],
    'ecs': [('mock-cluster/mock-api-svc', 'mock-api-svc', '')],
    'lambda_fn': [('mock-image-resizer', 'mock-image-resizer', '256 MB')],
    'vm': [
        ('/subscriptions/mock/resourceGroups/mock-rg/providers/'
         'Microsoft.Compute/virtualMachines/mock-vm-01', 'mock-vm-01', 'Standard_B1s'),
    ],
    'app_service': [
        ('/subscriptions/mock/resourceGroups/mock-rg/providers/'
         'Microsoft.Web/sites/mock-webapp', 'mock-webapp', 'B1'),
    ],
    'aks': [
        ('/subscriptions/mock/resourceGroups/mock-rg/providers/'
         'Microsoft.ContainerService/managedClusters/mock-aks', 'mock-aks', '2 nodes'),
    ],
    'sql_db': [
        ('/subscriptions/mock/resourceGroups/mock-rg/providers/'
         'Microsoft.Sql/servers/mock-sql/databases/mock-db', 'mock-db', 'S0'),
    ],
}


class MockManager(CloudManager):
    """Simulated provider. Actions always succeed; nothing leaves the process."""

    def __init__(self, provider_config: dict = None, provider: str = 'aws'):
        self.provider_config = provider_config or {}
        self.provider = provider
        self.region = self.provider_config.get('region', 'mock-region-1')

    def start_service(self, service) -> bool:
        logger.info(f"[mock] start {service.service_type} {service.cloud_resource_id}")
        service.current_status = 'running'
        return True

    def stop_service(self, service) -> bool:
        logger.info(f"[mock] stop {service.service_type} {service.cloud_resource_id}")
        service.current_status = 'stopped'
        return True

    def get_status(self, service) -> str:
        # Mock has no external truth to poll — the stored status IS the truth.
        return service.current_status or 'stopped'

    def list_resource_groups(self) -> list:
        return [{'name': 'mock-rg', 'location': self.region}]

    def list_resources(self, service_type: str, resource_group: str = None,
                       region: str = None) -> list:
        return [
            {
                'id': rid,
                'name': name,
                'status': 'stopped',
                'location': region or self.region,
                'resource_group': 'mock-rg' if self.provider == 'azure' else '',
                'size': size,
            }
            for rid, name, size in _CATALOGUE.get(service_type, [])
        ]
