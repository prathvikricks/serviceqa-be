"""
AWS cloud service manager.
Handles start/stop/status for EC2, RDS, ECS.

boto3 is imported lazily so a mock-only deployment never needs the SDK
installed — only a project in 'real' mode ever constructs this class.
"""
import logging

from .cloud_manager import CloudManager

logger = logging.getLogger(__name__)

_MISSING_CREDS = ("AWS credentials missing in project config. Set Access Key ID "
                  "and Secret Access Key in Admin → Project settings.")


class AWSManager(CloudManager):
    def __init__(self, provider_config: dict):
        self.provider_config = provider_config
        self.region = provider_config.get('region', 'us-east-1')
        self.access_key_id = provider_config.get('access_key_id')
        self.secret_access_key = provider_config.get('secret_access_key')
        self._session = None

    def _require_creds(self):
        if not (self.access_key_id and self.secret_access_key):
            raise ValueError(_MISSING_CREDS)

    @property
    def session(self):
        if self._session is None:
            import boto3
            self._require_creds()
            self._session = boto3.Session(
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                region_name=self.region,
            )
        return self._session

    def _get_client(self, service_name, region=None):
        """Client for the project's default region, or a specific region when
        given (same credentials). Lets discovery and start/stop reach resources
        in any region, not just the project default."""
        if region and region != self.region:
            import boto3
            self._require_creds()
            return boto3.client(
                service_name, aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key, region_name=region)
        return self.session.client(service_name)

    @staticmethod
    def _svc_region(service):
        """The region a saved service lives in (captured at discovery)."""
        return (getattr(service, 'cloud_config', None) or {}).get('region')

    def list_resource_groups(self) -> list:
        """AWS doesn't have resource groups — return current region as a single entry."""
        return [{'name': self.region, 'location': self.region}]

    def list_resources(self, service_type: str, resource_group: str = None,
                       region: str = None) -> list:
        dispatch = {
            'ec2': self._list_ec2,
            'rds': self._list_rds,
            'ecs': self._list_ecs,
            'lambda_fn': self._list_lambda,
        }
        handler = dispatch.get(service_type)
        if not handler:
            return []
        return handler(region)  # let errors surface so the endpoint can report them

    def _list_ec2(self, region=None):
        ec2 = self._get_client('ec2', region)
        response = ec2.describe_instances()
        results = []
        for res in response.get('Reservations', []):
            for inst in res.get('Instances', []):
                name = ''
                for tag in inst.get('Tags', []):
                    if tag['Key'] == 'Name':
                        name = tag['Value']
                state = inst['State']['Name']
                results.append({
                    'id': inst['InstanceId'],
                    'name': name or inst['InstanceId'],
                    'status': 'running' if state == 'running' else 'stopped' if state == 'stopped' else state,
                    'location': inst.get('Placement', {}).get('AvailabilityZone', ''),
                    'resource_group': '',
                    'size': inst.get('InstanceType', ''),
                })
        return results

    def _list_rds(self, region=None):
        rds = self._get_client('rds', region)
        response = rds.describe_db_instances()
        results = []
        for db in response.get('DBInstances', []):
            results.append({
                'id': db['DBInstanceIdentifier'],
                'name': db['DBInstanceIdentifier'],
                'status': 'running' if db['DBInstanceStatus'] == 'available' else db['DBInstanceStatus'],
                'location': db.get('AvailabilityZone', ''),
                'resource_group': '',
                'size': db.get('DBInstanceClass', ''),
                'version': db.get('EngineVersion', ''),
            })
        return results

    def _list_lambda(self, region=None):
        client = self._get_client('lambda', region)
        results = []
        paginator = client.get_paginator('list_functions')
        for page in paginator.paginate():
            for fn in page.get('Functions', []):
                results.append({
                    'id': fn['FunctionName'],
                    'name': fn['FunctionName'],
                    'status': 'running',
                    'location': region or self.region,
                    'resource_group': '',
                    'size': f"{fn.get('MemorySize', '')} MB",
                    'version': fn.get('Runtime', ''),
                })
        return results

    def _list_ecs(self, region=None):
        ecs = self._get_client('ecs', region)
        clusters = ecs.list_clusters().get('clusterArns', [])
        results = []
        for cluster_arn in clusters:
            services = ecs.list_services(cluster=cluster_arn).get('serviceArns', [])
            if services:
                details = ecs.describe_services(cluster=cluster_arn, services=services)
                for svc in details.get('services', []):
                    results.append({
                        'id': svc['serviceArn'],
                        'name': svc['serviceName'],
                        'status': 'running' if svc['runningCount'] > 0 else 'stopped',
                        'location': '',
                        'resource_group': cluster_arn.split('/')[-1],
                    })
        return results

    def start_service(self, service) -> bool:
        dispatch = {
            'ec2': self._start_ec2,
            'rds': self._start_rds,
            'ecs': self._start_ecs,
        }
        handler = dispatch.get(service.service_type)
        if not handler:
            logger.error(f"Unknown AWS service type: {service.service_type}")
            return False
        return handler(service)

    def stop_service(self, service) -> bool:
        dispatch = {
            'ec2': self._stop_ec2,
            'rds': self._stop_rds,
            'ecs': self._stop_ecs,
        }
        handler = dispatch.get(service.service_type)
        if not handler:
            logger.error(f"Unknown AWS service type: {service.service_type}")
            return False
        return handler(service)

    def get_status(self, service) -> str:
        dispatch = {
            'ec2': self._status_ec2,
            'rds': self._status_rds,
            'ecs': self._status_ecs,
        }
        handler = dispatch.get(service.service_type)
        if not handler:
            return 'unknown'
        try:
            return handler(service)
        except Exception as e:
            logger.error(f"Failed to get status for {service.name}: {e}")
            return 'unknown'

    # --- EC2 ---
    def _start_ec2(self, service) -> bool:
        ec2 = self._get_client('ec2', self._svc_region(service))
        instance_id = service.cloud_resource_id
        logger.info(f"Starting EC2 instance {instance_id}")
        ec2.start_instances(InstanceIds=[instance_id])
        return True

    def _stop_ec2(self, service) -> bool:
        ec2 = self._get_client('ec2', self._svc_region(service))
        instance_id = service.cloud_resource_id
        logger.info(f"Stopping EC2 instance {instance_id}")
        ec2.stop_instances(InstanceIds=[instance_id])
        return True

    def _status_ec2(self, service) -> str:
        ec2 = self._get_client('ec2', self._svc_region(service))
        instance_id = service.cloud_resource_id
        response = ec2.describe_instances(InstanceIds=[instance_id])
        state = response['Reservations'][0]['Instances'][0]['State']['Name']
        return 'running' if state == 'running' else 'stopped' if state == 'stopped' else state

    # --- RDS ---
    def _start_rds(self, service) -> bool:
        rds = self._get_client('rds', self._svc_region(service))
        db_id = service.cloud_resource_id
        logger.info(f"Starting RDS instance {db_id}")
        rds.start_db_instance(DBInstanceIdentifier=db_id)
        return True

    def _stop_rds(self, service) -> bool:
        rds = self._get_client('rds', self._svc_region(service))
        db_id = service.cloud_resource_id
        logger.info(f"Stopping RDS instance {db_id}")
        rds.stop_db_instance(DBInstanceIdentifier=db_id)
        return True

    def _status_rds(self, service) -> str:
        rds = self._get_client('rds', self._svc_region(service))
        db_id = service.cloud_resource_id
        response = rds.describe_db_instances(DBInstanceIdentifier=db_id)
        status = response['DBInstances'][0]['DBInstanceStatus']
        return 'running' if status == 'available' else 'stopped' if status == 'stopped' else status

    # --- ECS ---
    def _start_ecs(self, service) -> bool:
        ecs = self._get_client('ecs', self._svc_region(service))
        config = service.cloud_config or {}
        cluster = config.get('cluster')
        service_name = config.get('service_name')
        desired_count = config.get('desired_count', 1)
        logger.info(f"Starting ECS service {service_name} in cluster {cluster}")
        ecs.update_service(
            cluster=cluster,
            service=service_name,
            desiredCount=desired_count,
        )
        return True

    def _stop_ecs(self, service) -> bool:
        ecs = self._get_client('ecs', self._svc_region(service))
        config = service.cloud_config or {}
        cluster = config.get('cluster')
        service_name = config.get('service_name')
        logger.info(f"Stopping ECS service {service_name} in cluster {cluster}")
        ecs.update_service(
            cluster=cluster,
            service=service_name,
            desiredCount=0,
        )
        return True

    def _status_ecs(self, service) -> str:
        ecs = self._get_client('ecs', self._svc_region(service))
        config = service.cloud_config or {}
        cluster = config.get('cluster')
        service_name = config.get('service_name')
        response = ecs.describe_services(cluster=cluster, services=[service_name])
        if response['services']:
            svc = response['services'][0]
            return 'running' if svc['runningCount'] > 0 else 'stopped'
        return 'unknown'
