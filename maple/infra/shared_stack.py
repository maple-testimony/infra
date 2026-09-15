from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_autoscaling as autoscaling
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_rds as rds
from aws_cdk import aws_servicediscovery as sd
from constructs import Construct

from .api_gateway import ApiGateway

# The ECS-optimized Amazon Linux 2 ARM AMI the cluster instance is currently
# running, read from the deployed stack's resolved SsmParameterValue. us-east-1
# only, which is the sole region this app deploys to.
DEPLOYED_ECS_AMI = "ami-08195b29d3024785e"


class SharedStack(Stack):
    """Manages common resources used by all developers."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # A virtual private network that allows everything in Maple's cloud to
        # talk to each other.
        self.vpc: ec2.Vpc = ec2.Vpc(
            self,
            "VPC",
            vpc_name="maple-shared",
        )

        self.ssh_key_pair = ec2.CfnKeyPair(
            self, "SshKeyPair", key_name="maple-cluster-ssh-key"
        )

        # An AWS RDS Postgres instance. This instance contains all maple-related
        # production databases.
        self.create_rds_instance()

        # Create a "cluster" to run our workloads (the scraper and Typesense)
        self.create_cluster()

        # Create an API Gateway and Load balancer to allow users to interact with Maple.
        self.api: ApiGateway = ApiGateway(self, "Api", vpc=self.vpc)

    def create_cluster(self):
        self.cluster: ecs.Cluster = ecs.Cluster(
            self,
            "Cluster",
            vpc=self.vpc,
        )

        # Break-glass rollback: null in normal operation. Setting this to a
        # snapshot id and deploying replaces the cluster instance (the same
        # mechanism as any other launch-config change) and seeds it with that
        # snapshot's Typesense data. See #11 -- do not substitute another
        # restore mechanism; an AMI rebuild and a root-device override were
        # both tried and rejected, for reasons recorded there.
        #
        # Arming a PROD rollback is TWO coupled edits, not one. Set this key
        # AND put typesense_image_prod back to typesense/typesense:0.24.1.
        # Typesense 0.25 changed the on-disk format and v30 downgrades no
        # lower than v27, so a restored 0.24 data directory cannot be served
        # by 30.2, and re-pinning the tag without restoring the data cannot
        # read the directory 30.2 has already migrated. Either edit alone
        # leaves prod broken.
        restore_snapshot_id = self.node.try_get_context("search_restore_snapshot_id")

        capacity = self.cluster.add_capacity(
            "BaseCapacity",
            instance_type=ec2.InstanceType("t4g.large"),
            desired_capacity=1,
            key_name=self.ssh_key_pair.key_name,
            machine_image=ecs.EcsOptimizedImage.amazon_linux2(ecs.AmiHardwareType.ARM),
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            block_devices=(
                [
                    autoscaling.BlockDevice(
                        device_name="/dev/xvdb",
                        volume=autoscaling.BlockDeviceVolume.ebs_from_snapshot(
                            restore_snapshot_id, delete_on_termination=True
                        ),
                    )
                ]
                if restore_snapshot_id
                else None
            ),
        )

        # Tasks run in awsvpc mode, so without this they can reach the instance
        # metadata service and assume the EC2 instance role. CDK injected this
        # automatically until AWS deprecated and removed the mechanism, so it
        # has to be set explicitly to keep the pre-2.266 posture.
        #
        # These three lines and their order reproduce the deployed user data
        # byte for byte. That is load-bearing, not cosmetic: user data is a
        # LaunchConfiguration property, LaunchConfiguration has no mutable
        # properties, and the ASG replaces itself wholesale when its launch
        # configuration changes. Any drift here therefore destroys the instance,
        # taking both Typesense data volumes with it.
        capacity.add_user_data(
            "sudo iptables --insert FORWARD 1 --in-interface docker+ "
            "--destination 169.254.169.254/32 --jump DROP",
            "sudo service iptables save",
            "echo ECS_AWSVPC_BLOCK_IMDS=true >> /etc/ecs/ecs.config",
        )

        # Break-glass restore, kept as a separate call so the lines above stay
        # byte-identical when restore_snapshot_id is null. Stops the ECS agent
        # before copying so a starting task cannot race the copy for the same
        # RocksDB directory, then copies only the two Typesense docker volumes
        # off the attached snapshot -- not the whole /dev/xvdb tree -- so
        # nothing else on the restored root disk reaches the live instance.
        #
        # Both volumes are copied even for a prod-only rollback, deliberately.
        # The restore works by replacing the instance, and both volumes live on
        # that instance, so dev's data is destroyed whether or not it is
        # restored. Copying prod alone would leave dev empty and needing a full
        # reindex; copying both puts dev back where it already was, provided
        # the snapshot was taken shortly before the deploy being rolled back.
        if restore_snapshot_id:
            capacity.add_user_data(
                "sudo mkdir -p /mnt/restore",
                "sudo mount -o ro /dev/xvdb /mnt/restore",
                "sudo systemctl stop ecs",
                "sudo cp -a /mnt/restore/var/lib/docker/volumes/search-prod-data "
                "/var/lib/docker/volumes/",
                "sudo cp -a /mnt/restore/var/lib/docker/volumes/search-dev-data "
                "/var/lib/docker/volumes/",
                "sudo umount /mnt/restore",
                "sudo systemctl start ecs",
            )

        # The image is resolved from an SSM parameter that tracks the current
        # recommended ECS-optimized AMI, so CloudFormation re-resolves it on
        # every deploy and any AMI refresh silently replaces the instance. Pin
        # it to the AMI already running so that instance replacement is a
        # scheduled decision rather than a side effect of an unrelated deploy.
        # This is deliberately temporary and pairs with the data-durability work
        # in #7; refreshing it is safe only once the indexes outlive the host.
        capacity.node.find_child("LaunchConfig").add_property_override(
            "ImageId", DEPLOYED_ECS_AMI
        )

        self.cluster.connections.allow_from_any_ipv4(
            ec2.Port.tcp(22),
            "Allow SSH",
        )

        # Supports service discovery and routing api gateway requests to the
        # cluster.
        self.cluster.add_default_cloud_map_namespace(
            name="maple.net",
            type=sd.NamespaceType.DNS_PRIVATE,
        )

    def create_rds_instance(self):
        self.db_admin = rds.Credentials.from_generated_secret(
            "mapleadmin",
        )

        self.db = rds.DatabaseInstance(
            self,
            "PostgresInstance",
            engine=rds.DatabaseInstanceEngine.postgres(
                version=rds.PostgresEngineVersion.VER_14_6
            ),
            # db.t3.micro, 2cpu, 1g ram, $13/mo
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.BURSTABLE3, ec2.InstanceSize.MICRO
            ),
            database_name="maple",
            # start with 10g storage, expand up to 200g
            allocated_storage=10,
            max_allocated_storage=200,
            storage_type=rds.StorageType.GP2,
            vpc=self.vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            publicly_accessible=True,
            auto_minor_version_upgrade=True,
            enable_performance_insights=True,
            backup_retention=Duration.days(30),
            cloudwatch_logs_exports=["postgresql"],
            cloudwatch_logs_retention=logs.RetentionDays.TWO_MONTHS,
            removal_policy=RemovalPolicy.RETAIN,
            credentials=self.db_admin,
        )

        self.db_dev_role = iam.Role(
            self, "PostgresDevRole", assumed_by=iam.AccountRootPrincipal()
        )
        # self.db.grant_connect(self.db_dev_role)

        self.db.connections.allow_default_port_from_any_ipv4("Postgres Endpoint")
