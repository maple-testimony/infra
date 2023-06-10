import builtins
from typing import Literal

from aws_cdk import Stack
from aws_cdk import aws_apigatewayv2_alpha as apigw
from aws_cdk import aws_apigatewayv2_integrations_alpha as apigw_integrations
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_efs as efs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_elasticloadbalancingv2_targets as elbv2_targets
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_servicediscovery as sd
from constructs import Construct

from .api_gateway import ApiGateway, EnvName

service_names = {
    "dev": "search-dev",
    "prod": "search-prod",
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
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.create_service(cluster, service_names[env_name])

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
        self.definition: ecs.Ec2TaskDefinition = ecs.Ec2TaskDefinition(
            self,
            "SearchTaskDefinition",
            volumes=[volume],
            network_mode=ecs.NetworkMode.AWS_VPC,
        )

        # Add a container with environment variables and a mount point for the
        # EFS volume
        self.container: ecs.ContainerDefinition = self.definition.add_container(
            "TypesenseContainer",
            image=ecs.ContainerImage.from_registry(
                self.node.get_context("typesense_image")
            ),
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
        )

        self.service.connections.allow_from_any_ipv4(ec2.Port.all_traffic())
