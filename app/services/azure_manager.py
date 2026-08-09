"""
Azure cloud service manager.
Handles start/stop/status for Azure VMs, App Services, AKS, SQL databases.

The azure-mgmt SDKs are imported lazily so a mock-only deployment never needs
them — only a project in 'real' mode ever constructs this class.
"""
import logging
from .cloud_manager import CloudManager

logger = logging.getLogger(__name__)


class AzureManager(CloudManager):
    def __init__(self, provider_config: dict):
        self.provider_config = provider_config
        self.subscription_id = provider_config.get('subscription_id')
        self.tenant_id = provider_config.get('tenant_id')
        self.client_id = provider_config.get('client_id')
        self.client_secret = provider_config.get('client_secret')
        self._credential = None
        self._compute_client = None
        self._web_client = None
        self._sql_client = None
        self._container_client = None

    @property
    def credential(self):
        if self._credential is None:
            if not all([self.tenant_id, self.client_id, self.client_secret]):
                raise ValueError(
                    "Azure credentials missing in project config. "
                    "Set Tenant ID, Client ID, and Client Secret in Admin > Project settings."
                )
            try:
                from azure.identity import ClientSecretCredential
                self._credential = ClientSecretCredential(
                    tenant_id=self.tenant_id,
                    client_id=self.client_id,
                    client_secret=self.client_secret,
                )
            except Exception as e:
                logger.error(f"Failed to create Azure credential: {e}")
                raise
        return self._credential

    @property
    def compute_client(self):
        if self._compute_client is None:
            from azure.mgmt.compute import ComputeManagementClient
            self._compute_client = ComputeManagementClient(
                self.credential, self.subscription_id)
        return self._compute_client

    @property
    def web_client(self):
        if self._web_client is None:
            from azure.mgmt.web import WebSiteManagementClient
            self._web_client = WebSiteManagementClient(
                self.credential, self.subscription_id)
        return self._web_client

    @property
    def sql_client(self):
        if self._sql_client is None:
            from azure.mgmt.sql import SqlManagementClient
            self._sql_client = SqlManagementClient(
                self.credential, self.subscription_id)
        return self._sql_client

    @property
    def container_client(self):
        if self._container_client is None:
            from azure.mgmt.containerservice import ContainerServiceClient
            self._container_client = ContainerServiceClient(
                self.credential, self.subscription_id)
        return self._container_client

    def _parse_resource_id(self, resource_id: str) -> dict:
        """Parse an ARM resource ID into components."""
        parts = resource_id.strip('/').split('/')
        result = {}
        for i in range(0, len(parts) - 1, 2):
            result[parts[i].lower()] = parts[i + 1]
        return result

    def list_resource_groups(self) -> list:
        """List all resource groups in the subscription."""
        from azure.mgmt.resource import ResourceManagementClient
        resource_client = ResourceManagementClient(self.credential, self.subscription_id)
        results = []
        for rg in resource_client.resource_groups.list():
            results.append({
                'name': rg.name,
                'location': rg.location,
            })
        return sorted(results, key=lambda x: x['name'])

    def list_resources(self, service_type: str, resource_group: str = None,
                       region: str = None) -> list:
        """Discover Azure resources by type. Returns list of dicts with id, name,
        status, location. `region` is accepted for a uniform interface with AWS
        but ignored — Azure scopes by resource group, not region."""
        dispatch = {
            'vm': self._list_vms,
            'vmss': self._list_vmss,
            'app_service': self._list_app_services,
            'aks': self._list_aks,
            'sql_db': self._list_sql_dbs,
            'mysql': self._list_mysql,
            'container_app': self._list_app_services,
        }
        handler = dispatch.get(service_type)
        if not handler:
            logger.error(f"Cannot list resources for type: {service_type}")
            return []
        try:
            return handler(resource_group)
        except Exception as e:
            logger.error(f"Failed to list {service_type} resources: {e}")
            return []

    def _list_vms(self, resource_group=None):
        results = []
        if resource_group:
            vms = self.compute_client.virtual_machines.list(resource_group)
        else:
            vms = self.compute_client.virtual_machines.list_all()
        for vm in vms:
            status = 'unknown'
            try:
                parsed = self._parse_resource_id(vm.id)
                rg = parsed.get('resourcegroups')
                iv = self.compute_client.virtual_machines.instance_view(rg, vm.name)
                for s in iv.statuses:
                    if s.code.startswith('PowerState/'):
                        status = s.code.split('/')[-1]
            except Exception:
                pass
            results.append({
                'id': vm.id,
                'name': vm.name,
                'status': status,
                'location': vm.location,
                'resource_group': vm.id.split('/')[4] if vm.id else '',
                'size': vm.hardware_profile.vm_size if vm.hardware_profile else '',
            })
        return results

    def _list_vmss(self, resource_group=None):
        results = []
        if resource_group:
            items = self.compute_client.virtual_machine_scale_sets.list(resource_group)
        else:
            items = self.compute_client.virtual_machine_scale_sets.list_all()
        for vmss in items:
            results.append({
                'id': vmss.id,
                'name': vmss.name,
                'status': 'running' if vmss.provisioning_state == 'Succeeded' else vmss.provisioning_state,
                'location': vmss.location,
                'resource_group': vmss.id.split('/')[4] if vmss.id else '',
                'capacity': vmss.sku.capacity if vmss.sku else 0,
            })
        return results

    def _list_app_services(self, resource_group=None):
        results = []
        if resource_group:
            apps = self.web_client.web_apps.list_by_resource_group(resource_group)
        else:
            apps = self.web_client.web_apps.list()
        for app in apps:
            results.append({
                'id': app.id,
                'name': app.name,
                'status': 'running' if app.state == 'Running' else 'stopped',
                'location': app.location,
                'resource_group': app.resource_group,
                'kind': app.kind or 'app',
            })
        return results

    def _list_aks(self, resource_group=None):
        results = []
        if resource_group:
            clusters = self.container_client.managed_clusters.list_by_resource_group(resource_group)
        else:
            clusters = self.container_client.managed_clusters.list()
        for c in clusters:
            power = getattr(c, 'power_state', None)
            status = 'stopped' if power and power.code == 'Stopped' else 'running'
            results.append({
                'id': c.id,
                'name': c.name,
                'status': status,
                'location': c.location,
                'resource_group': c.id.split('/')[4] if c.id else '',
                'kubernetes_version': c.kubernetes_version,
            })
        return results

    def _list_sql_dbs(self, resource_group=None):
        from azure.mgmt.resource import ResourceManagementClient
        resource_client = ResourceManagementClient(self.credential, self.subscription_id)
        results = []
        # List SQL servers first, then databases
        if resource_group:
            servers = self.sql_client.servers.list_by_resource_group(resource_group)
        else:
            servers = self.sql_client.servers.list()
        for server in servers:
            rg = server.id.split('/')[4]
            dbs = self.sql_client.databases.list_by_server(rg, server.name)
            for db in dbs:
                if db.name == 'master':
                    continue
                results.append({
                    'id': db.id,
                    'name': f'{server.name}/{db.name}',
                    'status': 'running' if db.status == 'Online' else db.status.lower(),
                    'location': db.location,
                    'resource_group': rg,
                    'server': server.name,
                })
        return results

    def _list_mysql(self, resource_group=None):
        """List Azure Database for MySQL Flexible Servers."""
        results = []
        try:
            from azure.mgmt.rdbms.mysql_flexibleservers import MySQLManagementClient
            mysql_client = MySQLManagementClient(self.credential, self.subscription_id)
            if resource_group:
                servers = mysql_client.servers.list_by_resource_group(resource_group)
            else:
                servers = mysql_client.servers.list()
            for s in servers:
                results.append({
                    'id': s.id,
                    'name': s.name,
                    'status': 'running' if s.state == 'Ready' else 'stopped' if s.state == 'Stopped' else s.state,
                    'location': s.location,
                    'resource_group': s.id.split('/')[4] if s.id else '',
                    'version': str(s.version) if s.version else '',
                })
        except ImportError:
            logger.warning("azure-mgmt-rdbms not installed, cannot list MySQL servers")
        except Exception as e:
            logger.error(f"Failed to list MySQL servers: {e}")
        return results

    def start_service(self, service) -> bool:
        dispatch = {
            'vm': self._start_vm,
            'vmss': self._start_vmss,
            'app_service': self._start_app_service,
            'aks': self._start_aks,
            'sql_db': self._start_sql,
            'mysql': self._start_mysql,
            'container_app': self._start_app_service,
        }
        handler = dispatch.get(service.service_type)
        if not handler:
            logger.error(f"Unknown service type: {service.service_type}")
            return False
        return handler(service)

    def stop_service(self, service) -> bool:
        dispatch = {
            'vm': self._stop_vm,
            'vmss': self._stop_vmss,
            'app_service': self._stop_app_service,
            'aks': self._stop_aks,
            'sql_db': self._stop_sql,
            'mysql': self._stop_mysql,
            'container_app': self._stop_app_service,
        }
        handler = dispatch.get(service.service_type)
        if not handler:
            logger.error(f"Unknown service type: {service.service_type}")
            return False
        return handler(service)

    def get_status(self, service) -> str:
        dispatch = {
            'vm': self._status_vm,
            'vmss': self._status_vmss,
            'app_service': self._status_app_service,
            'aks': self._status_aks,
            'sql_db': self._status_sql,
            'mysql': self._status_mysql,
            'container_app': self._status_app_service,
        }
        handler = dispatch.get(service.service_type)
        if not handler:
            return 'unknown'
        try:
            return handler(service)
        except Exception as e:
            logger.error(f"Failed to get status for {service.name}: {e}")
            return 'unknown'

    # --- VM ---
    def _start_vm(self, service) -> bool:
        parsed = self._parse_resource_id(service.cloud_resource_id)
        rg = parsed.get('resourcegroups')
        vm_name = parsed.get('virtualmachines')
        logger.info(f"Starting VM {vm_name} in {rg}")
        poller = self.compute_client.virtual_machines.begin_start(rg, vm_name)
        poller.result()
        return True

    def _stop_vm(self, service) -> bool:
        parsed = self._parse_resource_id(service.cloud_resource_id)
        rg = parsed.get('resourcegroups')
        vm_name = parsed.get('virtualmachines')
        logger.info(f"Deallocating VM {vm_name} in {rg}")
        # Use deallocate, NOT power_off — deallocate stops billing
        poller = self.compute_client.virtual_machines.begin_deallocate(rg, vm_name)
        poller.result()
        return True

    def _status_vm(self, service) -> str:
        parsed = self._parse_resource_id(service.cloud_resource_id)
        rg = parsed.get('resourcegroups')
        vm_name = parsed.get('virtualmachines')
        instance = self.compute_client.virtual_machines.instance_view(rg, vm_name)
        for status in instance.statuses:
            if status.code.startswith('PowerState/'):
                state = status.code.split('/')[-1]
                return {'running': 'running', 'deallocated': 'deallocated',
                        'stopped': 'stopped'}.get(state, state)
        return 'unknown'

    # --- App Service ---
    def _start_app_service(self, service) -> bool:
        parsed = self._parse_resource_id(service.cloud_resource_id)
        rg = parsed.get('resourcegroups')
        app_name = parsed.get('sites')
        logger.info(f"Starting App Service {app_name} in {rg}")
        self.web_client.web_apps.start(rg, app_name)
        return True

    def _stop_app_service(self, service) -> bool:
        parsed = self._parse_resource_id(service.cloud_resource_id)
        rg = parsed.get('resourcegroups')
        app_name = parsed.get('sites')
        logger.info(f"Stopping App Service {app_name} in {rg}")
        self.web_client.web_apps.stop(rg, app_name)
        return True

    def _status_app_service(self, service) -> str:
        parsed = self._parse_resource_id(service.cloud_resource_id)
        rg = parsed.get('resourcegroups')
        app_name = parsed.get('sites')
        app = self.web_client.web_apps.get(rg, app_name)
        return 'running' if app.state == 'Running' else 'stopped'

    # --- AKS ---
    def _start_aks(self, service) -> bool:
        parsed = self._parse_resource_id(service.cloud_resource_id)
        rg = parsed.get('resourcegroups')
        config = service.cloud_config or {}
        cluster_name = config.get('cluster_name') or parsed.get('managedclusters')
        logger.info(f"Starting AKS cluster {cluster_name} in {rg}")
        poller = self.container_client.managed_clusters.begin_start(rg, cluster_name)
        poller.result()
        return True

    def _stop_aks(self, service) -> bool:
        parsed = self._parse_resource_id(service.cloud_resource_id)
        rg = parsed.get('resourcegroups')
        config = service.cloud_config or {}
        cluster_name = config.get('cluster_name') or parsed.get('managedclusters')
        logger.info(f"Stopping AKS cluster {cluster_name} in {rg}")
        poller = self.container_client.managed_clusters.begin_stop(rg, cluster_name)
        poller.result()
        return True

    def _status_aks(self, service) -> str:
        parsed = self._parse_resource_id(service.cloud_resource_id)
        rg = parsed.get('resourcegroups')
        config = service.cloud_config or {}
        cluster_name = config.get('cluster_name') or parsed.get('managedclusters')
        cluster = self.container_client.managed_clusters.get(rg, cluster_name)
        power_state = getattr(cluster, 'power_state', None)
        if power_state and power_state.code == 'Stopped':
            return 'stopped'
        return 'running'

    # --- SQL Database ---
    def _start_sql(self, service) -> bool:
        parsed = self._parse_resource_id(service.cloud_resource_id)
        rg = parsed.get('resourcegroups')
        server = parsed.get('servers')
        db_name = parsed.get('databases')
        logger.info(f"Resuming SQL DB {db_name} on {server}")
        poller = self.sql_client.databases.begin_resume(rg, server, db_name)
        poller.result()
        return True

    def _stop_sql(self, service) -> bool:
        parsed = self._parse_resource_id(service.cloud_resource_id)
        rg = parsed.get('resourcegroups')
        server = parsed.get('servers')
        db_name = parsed.get('databases')
        logger.info(f"Pausing SQL DB {db_name} on {server}")
        poller = self.sql_client.databases.begin_pause(rg, server, db_name)
        poller.result()
        return True

    def _status_sql(self, service) -> str:
        parsed = self._parse_resource_id(service.cloud_resource_id)
        rg = parsed.get('resourcegroups')
        server = parsed.get('servers')
        db_name = parsed.get('databases')
        db = self.sql_client.databases.get(rg, server, db_name)
        if db.status == 'Paused':
            return 'stopped'
        elif db.status == 'Online':
            return 'running'
        return db.status.lower()

    # --- VMSS ---
    def _start_vmss(self, service) -> bool:
        parsed = self._parse_resource_id(service.cloud_resource_id)
        rg = parsed.get('resourcegroups')
        vmss_name = parsed.get('virtualmachinescalesets')
        logger.info(f"Starting VMSS {vmss_name} in {rg}")
        poller = self.compute_client.virtual_machine_scale_sets.begin_start(rg, vmss_name)
        poller.result()
        return True

    def _stop_vmss(self, service) -> bool:
        parsed = self._parse_resource_id(service.cloud_resource_id)
        rg = parsed.get('resourcegroups')
        vmss_name = parsed.get('virtualmachinescalesets')
        logger.info(f"Deallocating VMSS {vmss_name} in {rg}")
        poller = self.compute_client.virtual_machine_scale_sets.begin_deallocate(rg, vmss_name)
        poller.result()
        return True

    def _status_vmss(self, service) -> str:
        parsed = self._parse_resource_id(service.cloud_resource_id)
        rg = parsed.get('resourcegroups')
        vmss_name = parsed.get('virtualmachinescalesets')
        vmss = self.compute_client.virtual_machine_scale_sets.get(rg, vmss_name)
        return 'running' if vmss.provisioning_state == 'Succeeded' else 'stopped'

    # --- MySQL Flexible Server ---
    def _start_mysql(self, service) -> bool:
        from azure.mgmt.rdbms.mysql_flexibleservers import MySQLManagementClient
        parsed = self._parse_resource_id(service.cloud_resource_id)
        rg = parsed.get('resourcegroups')
        server_name = parsed.get('flexibleservers') or parsed.get('servers')
        logger.info(f"Starting MySQL server {server_name} in {rg}")
        mysql_client = MySQLManagementClient(self.credential, self.subscription_id)
        poller = mysql_client.servers.begin_start(rg, server_name)
        poller.result()
        return True

    def _stop_mysql(self, service) -> bool:
        from azure.mgmt.rdbms.mysql_flexibleservers import MySQLManagementClient
        parsed = self._parse_resource_id(service.cloud_resource_id)
        rg = parsed.get('resourcegroups')
        server_name = parsed.get('flexibleservers') or parsed.get('servers')
        logger.info(f"Stopping MySQL server {server_name} in {rg}")
        mysql_client = MySQLManagementClient(self.credential, self.subscription_id)
        poller = mysql_client.servers.begin_stop(rg, server_name)
        poller.result()
        return True

    def _status_mysql(self, service) -> str:
        from azure.mgmt.rdbms.mysql_flexibleservers import MySQLManagementClient
        parsed = self._parse_resource_id(service.cloud_resource_id)
        rg = parsed.get('resourcegroups')
        server_name = parsed.get('flexibleservers') or parsed.get('servers')
        mysql_client = MySQLManagementClient(self.credential, self.subscription_id)
        server = mysql_client.servers.get(rg, server_name)
        if server.state == 'Ready':
            return 'running'
        elif server.state == 'Stopped':
            return 'stopped'
        return server.state.lower()
