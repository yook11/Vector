"""管理者が隔離runbookへ故障を注入し、作成所有権とcleanupを確認する。"""

import json
import time
import uuid

from probe import Probe, record


class FaultProbe(Probe):
    def fault_document(self):
        result = self.admin("ssm").get_document(
            Name=self.config["create_document"],
            DocumentVersion=self.config["create_version"],
        )
        content = json.loads(result["Content"])
        content["mainSteps"][0]["inputs"]["Script"] += """
_original_handler = handler
def handler(events, context):
    result = _original_handler(events, context)
    raise LifecycleError("InjectedLostCreateOutput")
"""
        name = self.fixture["prefix"] + "-fault-output"
        self.admin("ssm").create_document(
            Name=name,
            DocumentType="Automation",
            DocumentFormat="JSON",
            Content=json.dumps(content),
        )
        record("fault_document_created", document=name)
        return name

    def admin_start(self, name):
        result = self.admin("ssm").start_automation_execution(
            DocumentName=name, DocumentVersion="1", ClientToken=str(uuid.uuid4())
        )
        record("fault_execution_started", execution_id=result["AutomationExecutionId"])
        return result["AutomationExecutionId"]

    def failed_with_cleanup(self, name):
        result = self.wait(self.admin_start(name))
        if result["AutomationExecutionStatus"] != "Failed":
            raise RuntimeError("InjectedFailureWasHidden")
        return result

    def ownership(self):
        name = self.fault_document()
        executor_name = self.config["execution_role_arn"].rsplit("/", 1)[1]
        deny_name = "test-only-deny-termination"
        deny_added = False
        try:
            existing_id = self.current()["instance_id"]
            if not existing_id:
                raise RuntimeError("ExpectedExistingInstance")
            self.denied(
                "operations_cannot_run_fault_document",
                "ssm",
                "start_automation_execution",
                DocumentName=name,
                DocumentVersion="1",
            )
            self.failed_with_cleanup(name)
            if self.current()["instance_id"] != existing_id:
                raise RuntimeError("ReusedInstanceDeleted")
            record(
                "failed_reuse_preserves_existing", passed=True, instance_id=existing_id
            )
            self.finish()
            failed = self.failed_with_cleanup(name)
            ids = failed.get("Outputs", {}).get("cleanup.InstanceId", [])
            if (
                len(ids) != 1
                or not ids[0].startswith("i-")
                or self.current()["instance_id"] is not None
            ):
                raise RuntimeError("OwnedCleanupFailed")
            record(
                "lost_create_output_cleans_own_instance",
                passed=True,
                instance_id=ids[0],
            )
            self.admin("iam").put_role_policy(
                RoleName=executor_name,
                PolicyName=deny_name,
                PolicyDocument=json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Deny",
                                "Action": "ec2:TerminateInstances",
                                "Resource": f"arn:aws:ec2:{self.config['region']}:{self.config['account_id']}:instance/*",
                                "Condition": {
                                    "StringEquals": {
                                        "ec2:ResourceTag/vector:managed-bastion": self.fixture[
                                            "prefix"
                                        ]
                                    }
                                },
                            }
                        ],
                    }
                ),
            )
            deny_added = True
            time.sleep(15)
            failed = self.failed_with_cleanup(name)
            ids = failed.get("Outputs", {}).get("cleanup.InstanceId", [])
            current_id = self.current()["instance_id"]
            if ids != [current_id] or not current_id:
                raise RuntimeError("FailedCleanupLostInstanceId")
            record(
                "cleanup_failure_retains_instance_id",
                passed=True,
                instance_id=current_id,
            )
        finally:
            if deny_added:
                self.admin("iam").delete_role_policy(
                    RoleName=executor_name, PolicyName=deny_name
                )
                time.sleep(15)
            self.admin("ssm").delete_document(Name=name)
            record("fault_document_removed", document=name)

    def deletion_race(self):
        instance_id = self.current()["instance_id"]
        if not instance_id:
            raise RuntimeError("ExpectedExistingInstance")
        destroy_id = self.start("destroy", instance_id)
        ec2 = self.admin("ec2")
        for _ in range(30):
            value = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][
                0
            ]["Instances"][0]
            if value["State"]["Name"] == "shutting-down":
                break
            time.sleep(1)
        else:
            raise RuntimeError("DidNotObserveShuttingDown")
        create_id = self.start()
        destroyed = self.wait(destroy_id)
        created = self.wait(create_id)
        if destroyed["AutomationExecutionStatus"] != "Success":
            raise RuntimeError("RaceDestroyFailed")
        current_id = self.current()["instance_id"]
        if current_id == instance_id:
            raise RuntimeError("OldInstanceStillAttached")
        if created["AutomationExecutionStatus"] not in {"Success", "Failed"}:
            raise RuntimeError("UnexpectedCreateResult")
        if created["AutomationExecutionStatus"] == "Success" and not current_id:
            raise RuntimeError("SuccessorWasDeleted")
        record(
            "create_during_destroy",
            passed=True,
            destroyed_instance=instance_id,
            successor=current_id,
            create_status=created["AutomationExecutionStatus"],
        )
        if not current_id:
            result = self.wait(self.start())
            if result["AutomationExecutionStatus"] != "Success":
                raise RuntimeError("PostDestroyCreateFailed")

    def successor_after_lost_response(self):
        old_id = self.current()["instance_id"]
        if not old_id:
            raise RuntimeError("ExpectedExistingInstance")
        ssm, ec2 = self.admin("ssm"), self.admin("ec2")
        names = []
        try:
            for action, suffix, injected in [
                (
                    "destroy",
                    "fault-slow-destroy",
                    """
_original_wait_destroyed = wait_destroyed
def wait_destroyed(*args, **kwargs):
    time.sleep(120)
    return _original_wait_destroyed(*args, **kwargs)
""",
                ),
                (
                    "create",
                    "fault-launch-response",
                    """
_original_create = create
def create(ec2, config, execution_id, **kwargs):
    original_run = ec2.run_instances
    def lose_response(**request):
        original_run(**request)
        raise TimeoutError("InjectedLostLaunchResponse")
    ec2.run_instances = lose_response
    return _original_create(ec2, config, execution_id, **kwargs)
""",
                ),
            ]:
                content = json.loads(
                    ssm.get_document(
                        Name=self.config[action + "_document"],
                        DocumentVersion=self.config[action + "_version"],
                    )["Content"]
                )
                content["mainSteps"][0]["inputs"]["Script"] += injected
                if action == "destroy":
                    content["mainSteps"][0]["timeoutSeconds"] = 600
                name = self.fixture["prefix"] + "-" + suffix
                ssm.create_document(
                    Name=name,
                    DocumentType="Automation",
                    DocumentFormat="JSON",
                    Content=json.dumps(content),
                )
                names.append(name)
            destroy_id = ssm.start_automation_execution(
                DocumentName=names[0],
                DocumentVersion="1",
                Parameters={"InstanceId": [old_id]},
                ClientToken=str(uuid.uuid4()),
            )["AutomationExecutionId"]
            record(
                "delayed_destroy_started", execution_id=destroy_id, instance_id=old_id
            )
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                eni = ec2.describe_network_interfaces(
                    NetworkInterfaceIds=[self.config["network"]["network_interface_id"]]
                )["NetworkInterfaces"][0]
                if eni["Status"] == "available" and not eni.get("Attachment"):
                    break
                time.sleep(2)
            else:
                raise RuntimeError("OldInterfaceNotReleased")
            if (
                ssm.get_automation_execution(AutomationExecutionId=destroy_id)[
                    "AutomationExecution"
                ]["AutomationExecutionStatus"]
                != "InProgress"
            ):
                raise RuntimeError("MissedSuccessorOverlap")
            create_id = self.admin_start(names[1])
            created = self.wait(create_id)
            destroyed = self.wait(destroy_id)
            current_id = self.current()["instance_id"]
            if (
                created["AutomationExecutionStatus"] != "Success"
                or destroyed["AutomationExecutionStatus"] != "Success"
                or created["Outputs"].get("create.InstanceId") != [current_id]
                or not current_id
                or current_id == old_id
            ):
                raise RuntimeError("SuccessorOverlapFailed")
            matches = ec2.describe_instances(
                Filters=[{"Name": "client-token", "Values": [create_id]}]
            )["Reservations"]
            if sum(len(r["Instances"]) for r in matches) != 1:
                raise RuntimeError("DuplicateAfterLostResponse")
            record(
                "lost_launch_response_and_successor_preserved",
                passed=True,
                destroy_execution_id=destroy_id,
                create_execution_id=create_id,
                destroyed_instance=old_id,
                successor=current_id,
            )
        finally:
            for name in names:
                ssm.delete_document(Name=name)
                record("fault_document_removed", document=name)


if __name__ == "__main__":
    p = FaultProbe()
    p.ownership()
    p.deletion_race()
    p.successor_after_lost_response()
