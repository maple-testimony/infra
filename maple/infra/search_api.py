from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_integrations as apigw_integrations
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_servicediscovery as sd
from constructs import Construct

from .api_gateway import ApiGateway, EnvName

service_names = {
    "dev": "search-dev",
    "prod": "search-prod",
}

# (min_healthy_percent, max_healthy_percent) per environment.
#
# Typesense stores documents in RocksDB, which takes an exclusive lock on its
# data directory. Both tasks would land on the single container instance and
# mount the same shared Docker volume, so a replacement task that starts while
# the outgoing one is still running cannot open the data directory and exits.
# Neither service configures a deployment circuit breaker, so ECS would sit
# retrying that rather than failing fast. 0/100 forbids the overlap and forces
# stop-then-start, so a deployment cannot wedge on the lock.
#
# It removes that wedge and no other. desired_count is 1, so 0% healthy means
# the gap is a full outage for that environment rather than reduced capacity,
# and if the replacement task fails for any reason other than the lock -- an
# OOM against the container's memory limit, say -- the missing circuit breaker
# still leaves ECS retrying indefinitely with nothing serving.
deployment_percentages = {
    "dev": (0, 100),
    "prod": (0, 100),
}


class SearchApi(Construct):
    """Configures a Typesense instance running on an ECS Cluster."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        env_name: EnvName,
        api: ApiGateway,
        cluster: ecs.Cluster,
        image: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        min_healthy, max_healthy = deployment_percentages[env_name]
        self.create_service(
            cluster,
            service_names[env_name],
            image,
            min_healthy_percent=min_healthy,
            max_healthy_percent=max_healthy,
        )

        api.get(env_name).add_routes(
            path="/search/{route+}",
            methods=[apigw.HttpMethod.ANY],
            integration=apigw_integrations.HttpServiceDiscoveryIntegration(
                "SearchIntegration",
                self.service.cloud_map_service,
                vpc_link=api.vpc_link,
                parameter_mapping=apigw.ParameterMapping().overwrite_path(
                    apigw.MappingValue.custom("/${request.path.route}")
                ),
            ),
        )

    def create_service(
        self,
        cluster: ecs.Cluster,
        service_name: str,
        image: str,
        *,
        min_healthy_percent: int,
        max_healthy_percent: int,
    ):
        # Create a volume configuration for the EFS file system
        volume = ecs.Volume(
            name=f"{service_name}-data",
            docker_volume_configuration=ecs.DockerVolumeConfiguration(
                scope=ecs.Scope.SHARED,
                driver="local",
                autoprovision=True,
            ),
        )

        # Set up an admin api key
        self.api_key_secret = secretsmanager.Secret(
            self,
            "SearchApiAdminKeySecret",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                exclude_punctuation=True
            ),
        )

        # Create a Task Definition
        self.definition: ecs.TaskDefinition = ecs.TaskDefinition(
            self,
            "SearchTaskDefinition",
            compatibility=ecs.Compatibility.EC2,
            volumes=[volume],
            network_mode=ecs.NetworkMode.AWS_VPC,
        )

        # Add a container with environment variables and a mount point for the
        # EFS volume
        self.container: ecs.ContainerDefinition = self.definition.add_container(
            "TypesenseContainer",
            image=ecs.ContainerImage.from_registry(image),
            # entry_point=["bash"],
            # command=[
            #     "-c",
            #     "echo asdf && ls -la /app/data && touch /app/data/test.txt && ls -la /app/data",
            # ],
            memory_limit_mib=1024,
            logging=ecs.LogDriver.aws_logs(stream_prefix="search"),
            port_mappings=[ecs.PortMapping(container_port=8108)],
            environment={
                "TYPESENSE_DATA_DIR": "/app/data",
                "TYPESENSE_ENABLE_CORS": "true",
            },
            secrets={
                "TYPESENSE_API_KEY": ecs.Secret.from_secrets_manager(
                    self.api_key_secret
                ),
            },
        )

        self.container.add_mount_points(
            ecs.MountPoint(
                container_path="/app/data",
                source_volume=volume.name,
                read_only=False,
            )
        )

        # Create a service with a CloudMap service discovery entry matching the input id.
        # API Gateway uses this to route requests to the containers.
        self.service: ecs.Ec2Service = ecs.Ec2Service(
            self,
            service_name,
            cluster=cluster,
            desired_count=1,
            task_definition=self.definition,
            cloud_map_options=ecs.CloudMapOptions(
                name=service_name,
                dns_record_type=sd.DnsRecordType.SRV,
            ),
            min_healthy_percent=min_healthy_percent,
            max_healthy_percent=max_healthy_percent,
        )

        self.service.connections.allow_from_any_ipv4(ec2.Port.all_traffic())
