"""Synthesis regression tests.

These run without AWS credentials, so they are the dry-run harness for any
change to the infrastructure. The logical IDs asserted below were read out of
`cdk.out/assembly-Maple-App/MapleAppSharedStackE9F3A3D0.template.json`.
"""

import json
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from maple.infra.cicd_stack import CiCdStack
from maple.infra.shared_stack import DEPLOYED_ECS_AMI

REPO_ROOT = Path(__file__).parent.parent
# `cdk.App(context=...)` does not read cdk.json, so the feature flags pinned
# there must be merged in explicitly — otherwise these tests synthesize a
# differently-configured app than the CLI and the pipeline do.
FEATURE_FLAGS = json.loads((REPO_ROOT / "cdk.json").read_text())["context"]
CONTEXT = {
    **FEATURE_FLAGS,
    **json.loads((REPO_ROOT / "cdk.context.json").read_text()),
}

# CloudFormation logical IDs are derived from the construct tree path, so
# pinning them here fails the build if a construct is ever renamed or
# re-scoped. Renaming either secret would delete and recreate it, silently
# regenerating a live Typesense admin API key.
PROD_KEY_SECRET = "SearchApiSearchApiAdminKeySecretAE7C60F8"
DEV_KEY_SECRET = "DevSearchApiSearchApiAdminKeySecretE052B8E8"
PROD_TASK_DEFINITION = "SearchApiSearchTaskDefinition547D8A1B"
DEV_TASK_DEFINITION = "DevSearchApiSearchTaskDefinitionE5EA1B9E"
PROD_SERVICE = "SearchApisearchprodService3B81384A"
DEV_SERVICE = "DevSearchApisearchdevService86D221C4"


# The rollback gate. CONTEXT carries whatever this branch is set to; the two
# fixtures below pin each state explicitly so both are covered either way.
RESTORE_CONTEXT_KEY = "search_restore_snapshot_id"
RESTORE_SNAPSHOT_ID = "snap-0a340e5a52b71541d"


def _synth_shared_stack(context: dict) -> Template:
    """Synthesize the app the way app.py does and return the shared stack."""
    app = cdk.App(context=context)
    cicd = CiCdStack(
        app,
        "Maple",
        env=cdk.Environment(
            account=context["root_account_arn"],
            region=context["primary_region"],
        ),
    )
    stage = cicd.node.find_child("App")
    return Template.from_stack(stage.node.find_child("SharedStack"))


@pytest.fixture(scope="module")
def shared_stack_template() -> Template:
    return _synth_shared_stack(CONTEXT)


@pytest.fixture(scope="module")
def inert_shared_stack_template() -> Template:
    """The shared stack with the rollback gate explicitly off."""
    return _synth_shared_stack({**CONTEXT, RESTORE_CONTEXT_KEY: None})


@pytest.fixture(scope="module")
def restored_shared_stack_template() -> Template:
    """The shared stack synthesized with a rollback snapshot id set."""
    return _synth_shared_stack({**CONTEXT, RESTORE_CONTEXT_KEY: RESTORE_SNAPSHOT_ID})


def test_app_synthesizes(shared_stack_template: Template) -> None:
    assert shared_stack_template.to_json()["Resources"]


@pytest.mark.parametrize("logical_id", [PROD_KEY_SECRET, DEV_KEY_SECRET])
def test_admin_key_secret_logical_id_is_stable(
    shared_stack_template: Template, logical_id: str
) -> None:
    """Guard against regenerating a live Typesense admin API key."""
    resources = shared_stack_template.find_resources("AWS::SecretsManager::Secret")
    assert logical_id in resources, (
        f"{logical_id} is missing. Renaming or re-scoping the SearchApi / "
        f"DevSearchApi constructs or the SearchApiAdminKeySecret would delete "
        f"and recreate the secret, regenerating a live admin API key."
    )


@pytest.mark.parametrize(
    ("logical_id", "context_key"),
    [
        (PROD_TASK_DEFINITION, "typesense_image_prod"),
        (DEV_TASK_DEFINITION, "typesense_image_dev"),
    ],
)
def test_typesense_image_matches_context(
    shared_stack_template: Template, logical_id: str, context_key: str
) -> None:
    """Each environment pins its own image, so they can be upgraded separately."""
    resources = shared_stack_template.find_resources("AWS::ECS::TaskDefinition")
    assert logical_id in resources
    containers = resources[logical_id]["Properties"]["ContainerDefinitions"]
    assert [c["Image"] for c in containers] == [CONTEXT[context_key]]


@pytest.mark.parametrize("logical_id", [PROD_SERVICE, DEV_SERVICE])
def test_services_forbid_overlapping_tasks(
    shared_stack_template: Template, logical_id: str
) -> None:
    """Neither service may start a replacement task beside the outgoing one.

    Typesense holds an exclusive RocksDB lock on /app/data, both tasks land on
    the single container instance, and the volume is shared, so an overlapping
    replacement cannot open the data directory and exits. No deployment circuit
    breaker is configured, so ECS retries that rather than failing fast.
    """
    services = shared_stack_template.find_resources("AWS::ECS::Service")
    assert logical_id in services, (
        f"{logical_id} is missing. Renaming or re-scoping the SearchApi / "
        f"DevSearchApi constructs or their Ec2Service would replace the "
        f"service, and this assertion would stop checking anything."
    )
    # Asserted key by key rather than against the whole DeploymentConfiguration
    # so that adding a deployment circuit breaker later -- the mitigation the
    # comment in search_api.py names as missing -- does not fail this test.
    deployment = services[logical_id]["Properties"]["DeploymentConfiguration"]
    assert deployment["MinimumHealthyPercent"] == 0
    assert deployment["MaximumPercent"] == 100


def _launch_config(template: Template) -> dict:
    launch_configs = template.find_resources("AWS::AutoScaling::LaunchConfiguration")
    assert list(launch_configs) == ["ClusterBaseCapacityLaunchConfigA5D3E9A9"]
    return launch_configs["ClusterBaseCapacityLaunchConfigA5D3E9A9"]


# The deployed user data, split at the gate. LaunchConfiguration has no mutable
# properties, so the ASG replaces itself -- and both Typesense volumes with it --
# whenever any of this drifts. The restore lines are appended by a second
# add_user_data() call, so an armed branch is the base text plus that suffix and
# nothing else.
BASE_USER_DATA_TAIL = (
    " >> /etc/ecs/ecs.config\n"
    "sudo iptables --insert FORWARD 1 --in-interface docker+ "
    "--destination 169.254.169.254/32 --jump DROP\n"
    "sudo service iptables save\n"
    "echo ECS_AWSVPC_BLOCK_IMDS=true >> /etc/ecs/ecs.config"
)
RESTORE_USER_DATA_TAIL = (
    "\nsudo mkdir -p /mnt/restore\n"
    "sudo mount -o ro /dev/xvdb /mnt/restore\n"
    "sudo systemctl stop ecs\n"
    "sudo cp -a /mnt/restore/var/lib/docker/volumes/search-prod-data "
    "/var/lib/docker/volumes/\n"
    "sudo cp -a /mnt/restore/var/lib/docker/volumes/search-dev-data "
    "/var/lib/docker/volumes/\n"
    "sudo umount /mnt/restore\n"
    "sudo systemctl start ecs"
)


def _expected_user_data(restore_snapshot_id: str | None) -> dict:
    tail = BASE_USER_DATA_TAIL
    if restore_snapshot_id:
        tail += RESTORE_USER_DATA_TAIL
    return {
        "Fn::Base64": {
            "Fn::Join": [
                "",
                [
                    "#!/bin/bash\necho ECS_CLUSTER=",
                    {"Ref": "ClusterEB0386A7"},
                    tail,
                ],
            ]
        }
    }


def test_launch_configuration_is_byte_stable(
    shared_stack_template: Template,
) -> None:
    """Guard the cluster instance's launch configuration against drift.

    LaunchConfiguration has no mutable properties, so ANY change here replaces
    the ASG and the single container instance — destroying the shared Docker
    volumes that hold both Typesense indexes. shared_stack.py reproduces the
    deployed ImageId and user data byte for byte; this pins that claim.
    Treat a failure as a stop sign, not a snapshot to update.

    Arming the rollback deliberately breaks that byte-stability — replacing the
    instance is how the restore runs. So this asserts against whichever state
    this branch is in, and still catches any drift beyond it.
    """
    restore_snapshot_id = CONTEXT.get(RESTORE_CONTEXT_KEY)
    properties = _launch_config(shared_stack_template)["Properties"]
    assert properties["ImageId"] == DEPLOYED_ECS_AMI
    assert properties["UserData"] == _expected_user_data(restore_snapshot_id)
    assert ("BlockDeviceMappings" in properties) is bool(restore_snapshot_id)


def test_restore_is_inert_when_unset(inert_shared_stack_template: Template) -> None:
    """With the gate off the launch configuration is untouched.

    This is what makes the mechanism safe to carry on an unmerged branch, and
    safe to leave in place after a restore once the key goes back to null.
    """
    properties = _launch_config(inert_shared_stack_template)["Properties"]
    assert "BlockDeviceMappings" not in properties
    assert "/dev/xvdb" not in json.dumps(properties["UserData"])
    assert properties["UserData"] == _expected_user_data(None)


def test_restore_attaches_and_copies_when_set(
    restored_shared_stack_template: Template,
) -> None:
    """Setting the key attaches the snapshot and seeds both Typesense volumes."""
    properties = _launch_config(restored_shared_stack_template)["Properties"]

    mappings = properties["BlockDeviceMappings"]
    assert len(mappings) == 1
    assert mappings[0]["DeviceName"] == "/dev/xvdb"
    assert mappings[0]["Ebs"]["SnapshotId"] == RESTORE_SNAPSHOT_ID
    assert mappings[0]["Ebs"]["DeleteOnTermination"] is True

    assert properties["UserData"] == _expected_user_data(RESTORE_SNAPSHOT_ID)
