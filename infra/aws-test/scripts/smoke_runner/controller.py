"""試験設備の起動確認と、一括試験・結果回収・削除確認を管理する。"""

import argparse
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import sys
from contextlib import contextmanager
from datetime import UTC, datetime

from botocore.exceptions import ClientError

from . import assets, cleanup, readiness, remote, snapshot, testing
from .common import LOCAL, ROOT, client, execute, identity, save, session


def now():
    return datetime.now(UTC).isoformat()


class Run:
    def __init__(self, directory):
        self.directory = directory
        self.manifest = snapshot.verify(directory)
        self.inputs = snapshot.load(directory / "inputs.tfvars.json")
        self.backend = snapshot.load(directory / "backend.json")
        self.aws = session("vector-smoke-construction", directory / "aws.config")
        self.runner = session(self.manifest["runner_profile"], directory / "aws.config")
        self.result = (
            snapshot.load(directory / "result.json")
            if (directory / "result.json").exists()
            else {
                "run_id": directory.name,
                "started_at": now(),
                "inputs": self.inputs,
                "state": self.backend["bucket"] + "/" + self.backend["key"],
                "phases": {
                    name: {"status": "not_run"}
                    for name in [
                        "preflight",
                        "auth_schema",
                        "provision",
                        "database",
                        "test",
                        "collection",
                        "inventory",
                        "destroy",
                        "verification",
                    ]
                },
                "command_ids": [],
                "apply_attempted": False,
                "retained": [
                    "bootstrap IAM",
                    "state S3 and versions",
                    "ECR",
                    "SSM AI parameter",
                ],
            }
        )
        self.persist()

    def persist(self):
        save(self.directory / "result.json", self.result)
        lines = [f"RunId: {self.directory.name}", f"State: {self.result['state']}"]
        lines.extend(
            f"{name}: {value['status']}"
            for name, value in self.result["phases"].items()
        )
        if latest := self.result.get("latest_test_attempt"):
            lines.append(f"最新試験結果: {latest}/result.json")
            attempt = self.result["test_attempts"][-1]
            lines.append(f"試験対象: {attempt['target']}")
        lines.append("常設基盤（IAM・state S3・ECR・SSM）は保持します。")
        (self.directory / "summary.txt").write_text("\n".join(lines) + "\n")

    @contextmanager
    def phase(self, name):
        previous_phase = getattr(self, "current_phase", None)
        self.current_phase = name
        self.result["phases"][name] = {"status": "running", "started_at": now()}
        self.persist()
        print(f"{self.directory.name}: {name}", flush=True)
        try:
            yield
        except BaseException as error:
            self.result["phases"][name].update(
                status="failed", error_type=type(error).__name__
            )
            if isinstance(error, (RuntimeError, TimeoutError)):
                self.result["phases"][name]["reason"] = str(error)
            if isinstance(error, ClientError):
                self.result["phases"][name]["aws_error_code"] = error.response["Error"][
                    "Code"
                ]
            raise
        else:
            self.result["phases"][name]["status"] = "passed"
        finally:
            self.result["phases"][name]["finished_at"] = now()
            self.persist()
            self.current_phase = previous_phase

    def tf(self, args, log, timeout=300):
        execute(
            ["terraform", *args],
            cwd=self.directory / "workspace/infra/aws-test/smoke",
            log=self.directory / log,
            timeout=timeout,
            env=snapshot.environment(self.directory),
            progress=True,
        )

    def authenticate(self):
        identity(self.aws, self.inputs["expected_account_id"], "vector-test-terraform/")

    def claim(self, *, create=False):
        key = f"smoke/{self.directory.name}/owner.json"
        with client(self.aws, "s3") as s3:
            if create:
                try:
                    s3.put_object(
                        Bucket=self.backend["bucket"],
                        Key=key,
                        Body=self.manifest["owner_id"].encode(),
                        IfNoneMatch="*",
                    )
                except ClientError as error:
                    if error.response["Error"]["Code"] != "PreconditionFailed":
                        raise
            response = s3.get_object(Bucket=self.backend["bucket"], Key=key)
            with response["Body"] as body:
                owner = body.read(100).decode()
            if owner != self.manifest["owner_id"]:
                raise RuntimeError("run_owner_mismatch")

    def initialize(self):
        self.tf(
            [
                "init",
                "-input=false",
                "-lockfile=readonly",
                "-backend-config=" + str(self.directory / "backend.json"),
            ],
            "init.log",
            300,
        )

    def command(self, command_id):
        self.result["command_ids"].append(command_id)
        if getattr(self, "current_phase", None):
            self.result["phases"][self.current_phase].setdefault(
                "command_ids",
                [],
            ).append(command_id)
        self.persist()

    def outputs(self):
        self.tf(["output", "-json"], "outputs.json")
        raw = snapshot.load(self.directory / "outputs.json")
        return {key: value["value"] for key, value in raw.items()}

    def collect(self, *, destination=None):
        with self.phase("collection"):
            outputs_file = self.directory / "outputs.json"
            raw = snapshot.load(outputs_file) if outputs_file.exists() else {}
            prefix = "vector-test-" + self.directory.name
            # 出力が未確定の部分作成では、Terraformのログ名契約を使う。
            execution = raw.get("execution", {}).get(
                "value",
                {
                    "log_groups": {
                        **{
                            k: f"/vector-test/{prefix}/{k}"
                            for k in ["lambda", "runner", "proxy"]
                        },
                        "database": f"/aws/rds/instance/{prefix}/postgresql",
                    }
                },
            )
            destination = destination or self.directory / (
                "logs-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
            )
            destination.mkdir()
            self.result.setdefault("log_directories", []).append(
                str(destination.relative_to(self.directory))
            )
            self.persist()
            cleanup.collect_logs(self.runner, {"execution": execution}, destination)

    def destroy(self):
        if not self.result["apply_attempted"]:
            raise RuntimeError("this_command_has_not_started_provisioning")
        self.result["phases"]["verification"] = {"status": "not_run"}
        self.persist()
        with self.phase("cleanup_access"):
            self.authenticate()
            self.claim()
        previous = self.directory / "inventory.json"
        known = snapshot.load(previous) if previous.exists() else {}
        # 残存照会に失敗しても削除を試み、照会失敗そのものはレポートへ残す。
        try:
            with self.phase("inventory"):
                self.tf(["show", "-json"], "state-before-destroy.json")
                known = cleanup.remember_state(
                    snapshot.load(self.directory / "state-before-destroy.json"), known
                )
                save(previous, known)
                known = cleanup.inventory(self.aws, self.directory.name, known)
                save(previous, known)
        except Exception:
            print(
                "削除前の照会に失敗しました。記録を残して削除を試みます。", flush=True
            )
        with self.phase("destroy"):
            self.authenticate()
            self.initialize()
            self.tf(
                [
                    "plan",
                    "-destroy",
                    "-input=false",
                    "-lock-timeout=60s",
                    "-var-file=" + str(self.directory / "inputs.tfvars.json"),
                    "-out=" + str(self.directory / "destroy.tfplan"),
                ],
                "destroy-plan.log",
                600,
            )
            self.tf(
                [
                    "apply",
                    "-input=false",
                    "-lock-timeout=60s",
                    str(self.directory / "destroy.tfplan"),
                ],
                "destroy.log",
                5400,
            )
        with self.phase("verification"):
            self.tf(["state", "list"], "state-after-destroy.txt")
            if (self.directory / "state-after-destroy.txt").read_text().strip():
                raise RuntimeError("terraform_state_not_empty")
            cleanup.verify_deleted(self.aws, self.directory.name, known, self.directory)
            if self.result["phases"]["inventory"]["status"] != "passed":
                raise RuntimeError("pre_destroy_inventory_unconfirmed")

    def preflight(self, *, resume=False):
        with self.phase("preflight"):
            self.authenticate()
            identity(
                self.runner,
                self.inputs["expected_account_id"],
                "AWSReservedSSO_VectorTestRunner_",
            )
            with client(self.aws, "ecr") as ecr:
                for kind in ["backend", "proxy"]:
                    ecr.describe_images(
                        repositoryName=f"vector-test/{kind}",
                        imageIds=[{"imageDigest": self.inputs[f"{kind}_image_digest"]}],
                    )
            if not resume:
                self.ensure_new_state()
            self.claim(create=not resume)
            self.initialize()

    def ensure_new_state(self):
        with client(self.aws, "s3") as s3:
            matches = s3.list_objects_v2(
                Bucket=self.backend["bucket"], Prefix=self.backend["key"]
            ).get("Contents", [])
            if any(item["Key"] == self.backend["key"] for item in matches):
                raise RuntimeError("run_state_already_exists")

    def provision(self):
        with self.phase("provision"):
            self.tf(
                [
                    "plan",
                    "-input=false",
                    "-lock-timeout=60s",
                    "-var-file=" + str(self.directory / "inputs.tfvars.json"),
                    "-out=" + str(self.directory / "create.tfplan"),
                ],
                "create-plan.log",
                600,
            )
            self.tf(
                ["show", "-json", str(self.directory / "create.tfplan")],
                "create-plan.json",
            )
            plan = snapshot.load(self.directory / "create-plan.json")
            if any(
                change["mode"] == "managed"
                and change["change"]["actions"] != ["create"]
                for change in plan.get("resource_changes", [])
            ):
                raise RuntimeError("new_run_plan_contains_existing_resources")
            self.result["apply_attempted"] = True
            self.persist()
            self.tf(
                [
                    "apply",
                    "-input=false",
                    "-lock-timeout=60s",
                    str(self.directory / "create.tfplan"),
                ],
                "create.log",
                2700,
            )
            self.result["apply_completed"] = True
            self.persist()

    def verify_resources(self):
        with self.phase("resources"):
            outputs = self.outputs()
            expected = {
                "account_id": self.inputs["expected_account_id"],
                "region": "ap-northeast-1",
                "run_id": self.directory.name,
                "state_bucket": self.backend["bucket"],
                "state_key": self.backend["key"],
                **{
                    key: self.inputs[key]
                    for key in (
                        "backend_image_digest",
                        "proxy_image_digest",
                        "source_revision",
                    )
                },
            }
            if any(outputs["run"].get(k) != v for k, v in expected.items()):
                raise RuntimeError("saved_output_scope_mismatch")
            previous = self.directory / "inventory.json"
            known = snapshot.load(previous) if previous.exists() else {}
            self.tf(["show", "-json"], "state-after-create.json")
            known = cleanup.remember_state(
                snapshot.load(self.directory / "state-after-create.json"),
                known,
            )
            save(previous, known)
            actual = cleanup.inventory(self.aws, self.directory.name, known)
            save(previous, actual)
            resources = outputs["resources"]
            required = {
                "Vpcs": ("VpcId", [resources["vpc"]]),
                "Subnets": ("SubnetId", resources["subnets"].values()),
                "RouteTables": ("RouteTableId", resources["route_tables"].values()),
                "InternetGateways": (
                    "InternetGatewayId",
                    [resources["internet_gateway"]],
                ),
                "SecurityGroups": ("GroupId", resources["security_groups"].values()),
                "VpcEndpoints": (
                    "VpcEndpointId",
                    [
                        resources[k]
                        for k in (
                            "ssm_endpoint",
                            "ssmmessages_endpoint",
                        )
                        if k in resources
                    ],
                ),
                "Instances": ("InstanceId", resources["instances"].values()),
                "Volumes": ("VolumeId", resources["root_volumes"].values()),
                "DBInstances": ("DBInstanceIdentifier", [resources["rds"]]),
                "EventSourceMappings": (None, [resources["event_source_mapping"]]),
                "Roles": (None, resources["roles"].keys()),
                "Profiles": (None, resources["instance_profiles"].keys()),
                "Logs": (None, resources["log_groups"].values()),
                "Secrets": (None, [resources["master_secret_arn"]]),
                "Queue": (None, ["vector-test-" + self.directory.name]),
            }
            for kind, (field, ids) in required.items():
                present = {i[field] if field else i for i in actual[kind]}
                if not set(ids) <= present:
                    raise RuntimeError(f"runtime_resources_missing:{kind}")
            with client(self.aws, "lambda") as api:
                function = api.get_function(
                    FunctionName=outputs["execution"]["lambda_name"],
                )
            if function["Code"]["ResolvedImageUri"] != outputs["run"]["backend_image"]:
                raise RuntimeError("lambda_image_digest_mismatch")
            return outputs

    def up(self):
        attempts = self.result.setdefault("up_attempts", [])
        attempt = {"number": len(attempts) + 1, "started_at": now()}
        attempts.append(attempt)
        for name in ("preflight", "resources", *readiness.PHASES):
            self.result["phases"][name] = {"status": "not_run"}
        access_confirmed = False
        try:
            with self.phase("up"):
                if any(
                    self.result["phases"].get(name, {}).get("status", "not_run")
                    != "not_run"
                    for name in ("cleanup_access", "destroy", "verification")
                ):
                    raise RuntimeError("deletion_started_use_new_run_id")
                # 旧レポートでも、構築工程の合格が記録されていれば確認だけを許可する。
                completed = self.result.get("apply_completed", False) or (
                    self.result["phases"]["provision"]["status"] == "passed"
                )
                if self.result["apply_attempted"] and not completed:
                    raise RuntimeError(
                        "provision_unconfirmed_destroy_then_use_new_run_id"
                    )
                self.preflight(resume=completed)
                access_confirmed = True
                if not completed:
                    self.provision()
                outputs = self.verify_resources()
                readiness.check(
                    self.aws, self.runner, outputs, self.phase, self.command
                )
        except BaseException:
            if access_confirmed and self.result["apply_attempted"]:
                try:
                    self.collect()
                except Exception:
                    print(
                        "ログ回収が不完全です。起動失敗の記録と環境を保持します。",
                        flush=True,
                    )
            raise
        finally:
            attempt.update(
                finished_at=now(),
                status=self.result["phases"]["up"]["status"],
            )
            self.persist()
            save(
                self.directory / "up-attempts" / str(attempt["number"]) / "result.json",
                self.result,
            )
            print(
                f"RunId: {self.directory.name}\n結果保存先: {self.directory}\n"
                "upは環境を自動削除しません。作成済み設備は保持されます。\n"
                f"削除: make aws-smoke-destroy RUN_ID={self.directory.name}",
                flush=True,
            )

    def prepare_database(self, outputs):
        with self.phase("image"):
            self.result["image"] = remote.ensure_image(
                self.runner, outputs, self.command
            )
        with self.phase("database"):
            self.result["database"] = remote.prepare(
                self.runner, outputs, self.directory, self.command
            )

    def prepare(self):
        attempts = self.result.setdefault("prepare_attempts", [])
        attempt = {"number": len(attempts) + 1, "started_at": now()}
        attempts.append(attempt)
        for name in (
            "preflight",
            "resources",
            *readiness.PHASES,
            "auth_schema",
            "image",
            "database",
        ):
            self.result["phases"][name] = {"status": "not_run"}
        for name in ("database", "image"):
            self.result.pop(name, None)
        access_confirmed = False
        try:
            with self.phase("prepare"):
                if any(
                    self.result["phases"].get(name, {}).get("status", "not_run")
                    != "not_run"
                    for name in ("cleanup_access", "destroy", "verification")
                ):
                    raise RuntimeError("deletion_started_use_new_run_id")
                if not self.result.get("apply_completed") or (
                    self.result["phases"].get("up", {}).get("status") != "passed"
                ):
                    raise RuntimeError("up_must_pass_before_prepare")
                self.preflight(resume=True)
                access_confirmed = True
                outputs = self.verify_resources()
                readiness.check(
                    self.aws, self.runner, outputs, self.phase, self.command
                )
                with self.phase("auth_schema"):
                    assets.prepare_assets(
                        self.directory, self.inputs["source_revision"]
                    )
                self.prepare_database(outputs)
        except BaseException:
            if access_confirmed:
                try:
                    self.collect()
                except Exception:
                    print(
                        "ログ回収が不完全です。準備失敗の記録と環境を保持します。",
                        flush=True,
                    )
            raise
        finally:
            attempt.update(
                finished_at=now(), status=self.result["phases"]["prepare"]["status"]
            )
            self.persist()
            save(
                self.directory
                / "prepare-attempts"
                / str(attempt["number"])
                / "result.json",
                self.result,
            )
            failed = [
                name
                for name, value in self.result["phases"].items()
                if value["status"] == "failed"
            ]
            print(
                f"RunId: {self.directory.name}\n結果保存先: {self.directory}\n"
                f"prepare: {attempt['status']}\n"
                f"失敗工程: {', '.join(failed) or 'なし'}\n"
                "prepareは環境を自動削除しません。\n"
                f"再実行: make aws-smoke-prepare RUN_ID={self.directory.name}\n"
                f"削除: make aws-smoke-destroy RUN_ID={self.directory.name}",
                flush=True,
            )

    def require_prepared(self):
        if any(
            self.result["phases"].get(name, {}).get("status", "not_run") != "not_run"
            for name in ("cleanup_access", "destroy", "verification")
        ):
            raise RuntimeError("deletion_started_use_new_run_id")
        if not self.result.get("apply_completed") or any(
            self.result["phases"].get(name, {}).get("status") != "passed"
            for name in ("up", "prepare", "database")
        ):
            raise RuntimeError("prepare_must_pass_before_test")

    def test(self, target, timeout=300):
        self.execute_tests(target, timeout, create=False)

    def run(self, target, timeout=300):
        self.execute_tests(target, timeout, create=True)

    def execute_tests(self, target, timeout, *, create):
        attempt = testing.TestAttempt(self.directory, target, timeout)
        attempt.result.update(
            run_id=self.directory.name,
            source_revision=self.inputs["source_revision"],
            backend_image_digest=self.inputs["backend_image_digest"],
        )
        self.result.setdefault("test_attempts", []).append(attempt.result)
        self.result["latest_test_attempt"] = str(
            attempt.path.relative_to(self.directory)
        )
        self.result["tests"] = []
        for name in (
            "test_selection",
            "test",
            "preflight",
            "resources",
            *readiness.PHASES,
            "collection",
        ):
            self.result["phases"][name] = {"status": "not_run"}
        access_confirmed = False
        failed = False
        try:
            with self.phase("test_run"):
                try:
                    with self.phase("test_selection"):
                        if not create:
                            self.require_prepared()
                        attempt.collect()
                    if create:
                        self.preflight()
                    else:
                        self.preflight(resume=True)
                    access_confirmed = True
                    if create:
                        with self.phase("auth_schema"):
                            assets.prepare_assets(
                                self.directory, self.inputs["source_revision"]
                            )
                        self.provision()
                    outputs = self.verify_resources()
                    readiness.check(
                        self.aws, self.runner, outputs, self.phase, self.command
                    )
                    if create:
                        self.prepare_database(outputs)
                    with self.phase("test"):
                        attempt.execute(self.manifest["runner_profile"])
                except BaseException:
                    failed = True
                    raise
                finally:
                    try:
                        if access_confirmed and self.result["apply_attempted"]:
                            try:
                                self.collect(destination=attempt.path / "logs")
                            except Exception:
                                if not failed:
                                    raise
                                print(
                                    "結果回収が不完全です。元の失敗と回収結果を記録します。",
                                    flush=True,
                                )
                    finally:
                        if create and self.result["apply_attempted"]:
                            self.destroy()
        except BaseException as error:
            attempt.result["error_type"] = type(error).__name__
            raise
        finally:
            try:
                self.result["tests"] = attempt.cases()
            except (OSError, ValueError) as error:
                self.result["tests"] = []
                attempt.result["report_error_type"] = type(error).__name__
            attempt.result.update(
                status=self.result["phases"]["test_run"]["status"],
                finished_at=now(),
                log_collection=dict(self.result["phases"]["collection"]),
            )
            attempt.persist()
            if create:
                self.result["finished_at"] = now()
            self.persist()
            print(
                f"RunId: {self.directory.name}\n対象: {attempt.target}\n"
                f"結果保存先: {attempt.path}\n状態: {attempt.result['status']}\n",
                flush=True,
            )
            if not create:
                print(
                    "testは環境を自動削除しません。\n"
                    f"再実行: make aws-smoke-test RUN_ID={self.directory.name} "
                    f"TEST={shlex.quote(attempt.target)} TIMEOUT={attempt.timeout}\n"
                    f"削除: make aws-smoke-destroy RUN_ID={self.directory.name}",
                    flush=True,
                )


def main():
    parser = argparse.ArgumentParser(
        description="試験AWSの構築・DB準備・試験・回収・削除確認"
    )
    parser.add_argument(
        "action", choices=["up", "prepare", "test", "run", "destroy", "status"]
    )
    parser.add_argument("--run-id", default=os.environ.get("RUN_ID"))
    parser.add_argument("--runner-profile", default="vector-test-runner")
    parser.add_argument(
        "--test", default=os.environ.get("TEST"), help="aws_tests配下の実行対象"
    )
    parser.add_argument(
        "--timeout", default=os.environ.get("TIMEOUT") or "300", help="試験の上限秒数"
    )
    args = parser.parse_args()
    if args.action in {"test", "run"}:
        try:
            args.test = testing.selector(args.test, ROOT)
            args.timeout = testing.timeout_seconds(args.timeout)
        except (ValueError, OSError) as error:
            parser.error(f"TESTまたはTIMEOUTが不正です: {type(error).__name__}")
    run_id = args.run_id or (
        datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        if args.action in {"up", "run"}
        else ""
    )
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", run_id) or len(run_id) > 24:
        parser.error("有効なRUN_ID（英小文字・数字・ハイフン、24文字以内）が必要です。")
    os.umask(0o077)
    directory = LOCAL / "runs" / run_id
    created = False
    if args.action in {"up", "run"}:
        try:
            directory.mkdir(parents=True, exist_ok=False)
            created = True
        except FileExistsError:
            if args.action != "up":
                raise
    if not directory.is_dir():
        parser.error("保存済みの実行ディレクトリがありません。")
    print(f"結果保存先: {directory}", flush=True)
    if args.action == "status" and (directory / "summary.txt").exists():
        print((directory / "summary.txt").read_text(), flush=True)
    # 同じ実行への並行操作は拒否し、別の削除プロセスを上書きしない。
    with (directory / "operation.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if created:
            try:
                snapshot.create(directory, run_id, args.runner_profile)
            except BaseException:
                # AWS操作前の失敗なので、この呼出が作成した不完全な入力だけを除去する。
                shutil.rmtree(directory)
                raise
        run = Run(directory)
        if args.action == "up":
            run.up()
        elif args.action == "prepare":
            run.prepare()
        elif args.action == "test":
            run.test(args.test, args.timeout)
        elif args.action == "run":
            run.run(args.test, args.timeout)
        elif args.action == "destroy":
            try:
                if run.result.get("up_attempts") or (
                    run.result["phases"].get("collection", {}).get("status") != "passed"
                ):
                    run.collect()
            except Exception:
                print("結果回収が不完全です。記録を残して削除へ進みます。", flush=True)
            finally:
                run.destroy()
        else:
            with run.phase("status"):
                run.authenticate()
                known_file = directory / "inventory.json"
                known = snapshot.load(known_file) if known_file.exists() else {}
                remaining = cleanup.inventory(run.aws, run_id, known)
                save(directory / "remaining.json", remaining)
                print(
                    json.dumps(
                        {k: len(v) for k, v in remaining.items()}, ensure_ascii=False
                    )
                )
                if any(remaining.values()):
                    raise RuntimeError("resources_remain")
    print((directory / "summary.txt").read_text(), flush=True)


def interrupted(signum, frame):
    raise KeyboardInterrupt


def entrypoint():
    signal.signal(signal.SIGTERM, interrupted)
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print(
            f"未完了: {type(error).__name__}。"
            "保存先のresult.jsonと工程ログを確認してください。",
            file=sys.stderr,
        )
        return 1
    return 0
