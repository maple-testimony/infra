import builtins

from aws_cdk import Stack
from aws_cdk import aws_apigatewayv2_alpha as apigw
from aws_cdk import aws_apigatewayv2_integrations_alpha as apigw_integrations
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_efs as efs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_elasticloadbalancingv2_targets as elbv2_targets
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from .api_construct import Api


class SearchTask(Construct):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        vpc: ec2.Vpc,
        cluster: ecs.Cluster,
    ) -> None:
        super().__init__(scope, id)

        # Create a new EFS file system
        efs_file_system: efs.FileSystem = efs.FileSystem(
            self,
            "EfsFileSystem",
            vpc=vpc,
            performance_mode=efs.PerformanceMode.GENERAL_PURPOSE,
        )
        efs_file_system.connections.allow_default_port_from(cluster)

        # Create a volume configuration for the EFS file system
        volume = ecs.Volume(
            name="efsVolume",
            docker_volume_configuration=ecs.DockerVolumeConfiguration(
                scope=ecs.Scope.SHARED,
                driver="local",
                autoprovision=True,
            )
            # efs_volume_configuration=ecs.EfsVolumeConfiguration(
            #     file_system_id=efs_file_system.file_system_id,
            # ),
        )

        # Set up an admin api key
        api_key_secret = secretsmanager.Secret(
            self,
            "SearchApiAdminKeySecret",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                exclude_punctuation=True
            ),
        )

        # Create a Task Definition
        self.definition: ecs.Ec2TaskDefinition = ecs.Ec2TaskDefinition(
            self,
            "TaskDef",
            volumes=[volume],
        )

        # Add a container with environment variables and a mount point for the
        # EFS volume
        container: ecs.ContainerDefinition = self.definition.add_container(
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
                "TYPESENSE_API_KEY": ecs.Secret.from_secrets_manager(api_key_secret),
            },
        )
        container.add_mount_points(
            ecs.MountPoint(
                container_path="/app/data",
                source_volume=volume.name,
                read_only=False,
            )
        )


class SearchStack(Stack):
    """Configures a Typesense instance running on an ECS Cluster."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        api: Api,
        cluster: ecs.Cluster,
        vpc: ec2.Vpc,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        search_task: SearchTask = SearchTask(
            self,
            "SearchTask",
            vpc=vpc,
            cluster=cluster,
        )

        # Create the service
        ecs_service: ecs.Ec2Service = ecs.Ec2Service(
            self,
            "SearchService",
            cluster=cluster,
            desired_count=1,
            task_definition=search_task.definition,
        )

        # Add a listener for the external port of the LB
        listener: elbv2.ApplicationListener = api.load_balancer.add_listener(
            "SearchListener",
            port=80,
            default_action=elbv2.ListenerAction.fixed_response(404),
        )

        listener.add_targets(
            "Search",
            conditions=[
                elbv2.ListenerCondition.path_patterns(
                    [
                        "/",
                        "/*",
                    ]
                )
            ],
            health_check=elbv2.HealthCheck(healthy_http_codes="200", path="/health"),
            port=80,
            targets=[ecs_service],
            priority=1,
        )

        api.http_api.add_routes(
            # API Gateway automatically strips off the /search prefix
            path="/search/{route+}",
            methods=[apigw.HttpMethod.ANY],
            integration=apigw_integrations.HttpAlbIntegration(
                "SearchIntegration",
                listener,
                vpc_link=api.vpc_link,
                parameter_mapping=apigw.ParameterMapping().overwrite_path(
                    apigw.MappingValue.custom("/${request.path.route}")
                ),
            ),
        )


# apiVersion: apps/v1
# kind: Deployment
# metadata:
#   name: typesense-deployment
#   namespace: {{.Release.Namespace}}
# spec:
#   selector:
#     matchLabels:
#       app: typesense
#   replicas: 1
#   strategy:
#     type: Recreate
#   template:
#     metadata:
#       labels:
#         app: typesense
#     spec:
#       containers:
#         - name: typesense
#           image: typesense/typesense:0.24.0
#           ports:
#             - containerPort: 8108
#           resources:
#             limits:
#               memory: 1Gi
#               cpu: "1"
#             requests:
#               memory: 50Mi
#               cpu: 50m
#           volumeMounts:
#             - mountPath: /app/data
#               name: datav
#           env:
#             - name: TYPESENSE_DATA_DIR
#               value: /app/data
#             - name: TYPESENSE_API_KEY
#               valueFrom:
#                 secretKeyRef:
#                   name: "{{ .Values.secretName }}"
#                   key: api-key
#             - name: TYPESENSE_ENABLE_CORS
#               value: "true"
#       volumes:
#         - name: datav
#           persistentVolumeClaim:
#             claimName: typesense-data
