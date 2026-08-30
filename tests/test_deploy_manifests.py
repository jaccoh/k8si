"""Structural assertions on the deploy/ manifests and CI workflow.

These are YAML round-trip + targeted assertions (no cluster needed). They pin
the deploy contract so a manifest edit that silently widens RBAC, moves the
dashboard namespace, or drops CRD validation fails review instead of prod.
"""

from pathlib import Path

import yaml

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
CI = Path(__file__).resolve().parent.parent / ".gitea" / "workflows" / "ci.yml"


def _docs(path: Path) -> list[dict]:
    """Parse a multi-document YAML manifest into a list of objects."""
    text = path.read_text()
    return [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]


def _find(docs: list[dict], kind: str, name: str) -> dict:
    for d in docs:
        if d.get("kind") == kind and d.get("metadata", {}).get("name") == name:
            return d
    raise AssertionError(f"no {kind} named {name!r} in manifest")


def _verbs(role: dict, api_group: str, resource: str) -> set[str]:
    """Return the verbs granted on (api_group, resource) by a Role/ClusterRole."""
    for rule in role.get("rules", []):
        if api_group in rule.get("apiGroups", []) and resource in rule.get("resources", []):
            return set(rule.get("verbs", []))
    return set()


# ── RBAC scoping ─────────────────────────────────────────────────────────────


class TestClusterWideRbac:
    """deploy/rbac.yaml stays the default, fully-working cluster-wide setup."""

    def test_operator_clusterrole_grants_secret_read(self):
        role = _find(_docs(DEPLOY / "rbac.yaml"), "ClusterRole", "k8si-operator")
        assert "get" in _verbs(role, "", "secrets")

    def test_operator_clusterrole_grants_pod_exec(self):
        role = _find(_docs(DEPLOY / "rbac.yaml"), "ClusterRole", "k8si-operator")
        assert "create" in _verbs(role, "", "pods/exec")

    def test_restore_clusterrole_exists_and_only_patches_status(self):
        role = _find(_docs(DEPLOY / "rbac.yaml"), "ClusterRole", "k8si-restore")
        assert _verbs(role, "k8si.io", "k8sibackups/status") == {"patch"}


class TestNamespacedRbac:
    """deploy/rbac-namespaced.yaml is the scoped alternative (goals-doc #10)."""

    @property
    def docs(self) -> list[dict]:
        return _docs(DEPLOY / "rbac-namespaced.yaml")

    def test_operator_clusterrole_no_longer_reads_secrets(self):
        role = _find(self.docs, "ClusterRole", "k8si-operator")
        assert _verbs(role, "", "secrets") == set(), "secrets-get must be namespaced"

    def test_operator_clusterrole_no_longer_creates_pod_exec(self):
        role = _find(self.docs, "ClusterRole", "k8si-operator")
        assert _verbs(role, "", "pods/exec") == set(), "pods/exec must be namespaced"

    def test_cluster_role_keeps_what_the_operator_truly_needs_cluster_wide(self):
        role = _find(self.docs, "ClusterRole", "k8si-operator")
        # k8si CRDs
        assert "k8sibackups" in role["rules"][0]["resources"]
        # jobs
        assert _verbs(role, "batch", "jobs")
        # pods read
        assert {"get", "list"} <= _verbs(role, "", "pods")
        # events
        assert _verbs(role, "", "events")
        # volumesnapshots
        assert _verbs(role, "snapshot.storage.k8s.io", "volumesnapshots")
        # PVC/PV still needed cluster-wide for an all-namespaces operator
        assert _verbs(role, "", "persistentvolumeclaims")
        assert _verbs(role, "", "persistentvolumes")

    def test_namespace_role_carries_secrets_get_and_pod_exec(self):
        role = _find(self.docs, "Role", "k8si-operator-namespace")
        assert "get" in _verbs(role, "", "secrets")
        assert "create" in _verbs(role, "", "pods/exec")
        # and nothing namespace-scoped that would be surprising
        assert _verbs(role, "", "pods") == {"get", "list"}
        assert "get" in _verbs(role, "", "pods/log")

    def test_namespace_role_is_bound_to_the_operator_serviceaccount(self):
        binding = _find(self.docs, "RoleBinding", "k8si-operator-namespace")
        assert binding["roleRef"]["kind"] == "Role"
        assert binding["roleRef"]["name"] == "k8si-operator-namespace"
        subjects = binding["subjects"]
        assert subjects and subjects[0]["kind"] == "ServiceAccount"
        assert subjects[0]["name"] == "k8si-operator"
        assert subjects[0]["namespace"] == "k8si-system"

    def test_operator_serviceaccount_and_namespace_present(self):
        _find(self.docs, "Namespace", "k8si-system")
        sa = _find(self.docs, "ServiceAccount", "k8si-operator")
        assert sa["metadata"]["namespace"] == "k8si-system"

    def test_scoped_variant_keeps_the_restore_clusterrole(self):
        role = _find(self.docs, "ClusterRole", "k8si-restore")
        assert _verbs(role, "k8si.io", "k8sibackups/status") == {"patch"}

    def test_scoped_variant_clusterrolebinding_targets_operator_sa(self):
        binding = _find(self.docs, "ClusterRoleBinding", "k8si-operator")
        assert binding["roleRef"]["name"] == "k8si-operator"
        assert binding["subjects"][0]["name"] == "k8si-operator"
        assert binding["subjects"][0]["namespace"] == "k8si-system"


# ── Dashboard exposure ────────────────────────────────────────────────────────


class TestUiNamespace:
    """deploy/ui.yaml must land in k8si-system — same namespace as the operator."""

    def test_ui_deployment_namespace_matches_operator(self):
        ui = _find(_docs(DEPLOY / "ui.yaml"), "Deployment", "k8si-ui")
        operator = _find(_docs(DEPLOY / "operator.yaml"), "Deployment", "k8si-operator")
        assert ui["metadata"]["namespace"] == operator["metadata"]["namespace"]

    def test_ui_service_account_namespace_matches_deployment(self):
        docs = _docs(DEPLOY / "ui.yaml")
        sa = _find(docs, "ServiceAccount", "k8si-ui")
        dep = _find(docs, "Deployment", "k8si-ui")
        assert sa["metadata"]["namespace"] == dep["metadata"]["namespace"]

    def test_ui_clusterrolebinding_subject_matches_sa_namespace(self):
        binding = _find(_docs(DEPLOY / "ui.yaml"), "ClusterRoleBinding", "k8si-ui")
        sa = _find(_docs(DEPLOY / "ui.yaml"), "ServiceAccount", "k8si-ui")
        assert binding["subjects"][0]["namespace"] == sa["metadata"]["namespace"]

    def test_ui_uses_its_service_account(self):
        dep = _find(_docs(DEPLOY / "ui.yaml"), "Deployment", "k8si-ui")
        assert dep["spec"]["template"]["spec"]["serviceAccountName"] == "k8si-ui"


class TestUiIngressVariant:
    """deploy/ui-ingress.yaml = ClusterIP + Ingress instead of NodePort."""

    @property
    def docs(self) -> list[dict]:
        return _docs(DEPLOY / "ui-ingress.yaml")

    def test_service_is_clusterip(self):
        svc = _find(self.docs, "Service", "k8si-ui")
        assert svc["spec"]["type"] == "ClusterIP"
        assert "nodePort" not in str(svc), "ClusterIP variant must not pin a nodePort"

    def test_service_selects_the_ui_pod(self):
        svc = _find(self.docs, "Service", "k8si-ui")
        assert svc["spec"]["selector"] == {"app": "k8si-ui"}
        ports = svc["spec"]["ports"]
        assert ports and ports[0]["port"] == 8080
        assert ports[0]["targetPort"] == 8080

    def test_service_namespace_matches_deployment(self):
        svc = _find(self.docs, "Service", "k8si-ui")
        dep = _find(_docs(DEPLOY / "ui.yaml"), "Deployment", "k8si-ui")
        assert svc["metadata"]["namespace"] == dep["metadata"]["namespace"]

    def test_ingress_routes_to_the_service(self):
        ing = _find(self.docs, "Ingress", "k8si-ui")
        assert ing["spec"]["ingressClassName"]
        rule = ing["spec"]["rules"][0]
        assert rule["http"]["paths"][0]["path"] == "/"
        assert rule["http"]["paths"][0]["pathType"] == "Prefix"
        backend = rule["http"]["paths"][0]["backend"]["service"]
        assert backend["name"] == "k8si-ui"
        assert backend["port"]["number"] == 8080

    def test_ingress_namespace_matches_service(self):
        ing = _find(self.docs, "Ingress", "k8si-ui")
        svc = _find(self.docs, "Service", "k8si-ui")
        assert ing["metadata"]["namespace"] == svc["metadata"]["namespace"]


# ── CRD validation ────────────────────────────────────────────────────────────


def _openapi(crd_path: Path, crd_name: str) -> dict:
    crd = _find(_docs(crd_path), "CustomResourceDefinition", crd_name)
    version = next(v for v in crd["spec"]["versions"] if v.get("storage"))
    return version["schema"]["openAPIV3Schema"]


def _assert_structural(node: dict, where: str = "root") -> None:
    """Recursive structural-schema sanity check.

    Mirrors the rules the API server enforces on CRD schemas: every node needs
    a `type`, objects need `properties` (or explicitly preserve unknown
    fields), arrays need `items`, and CEL/pattern constraints cannot sit next
    to `x-kubernetes-preserve-unknown-fields`.
    """
    preserve = bool(node.get("x-kubernetes-preserve-unknown-fields"))
    for key in ("x-kubernetes-validations", "pattern"):
        assert not (preserve and key in node), f"{where}: {key} beside preserve-unknown-fields"
    assert "type" in node, f"{where}: structural schemas require an explicit type"

    if node["type"] == "object":
        if not preserve:
            assert "properties" in node, f"{where}: object without properties"
            for key, child in node.get("properties", {}).items():
                _assert_structural(child, f"{where}.{key}")
    elif node["type"] == "array":
        assert "items" in node, f"{where}: array without items"
        _assert_structural(node["items"], f"{where}[]")


def _spec_props(crd_name: str) -> dict:
    crd = _find(_docs(DEPLOY / "crd.yaml"), "CustomResourceDefinition", crd_name)
    version = next(v for v in crd["spec"]["versions"] if v.get("storage"))
    return version["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]


def _status_props() -> dict:
    return _openapi(DEPLOY / "crd.yaml", "k8sibackups.k8si.io")["properties"]["status"][
        "properties"
    ]


class TestCrdScheduleValidation:
    """spec.schedule must be constrained to a cron-shaped string."""

    def test_schedule_has_pattern(self):
        assert _spec_props("k8sibackups.k8si.io")["schedule"].get("pattern")

    def test_schedule_pattern_accepts_plain_five_field_cron(self):
        import re

        pattern = _spec_props("k8sibackups.k8si.io")["schedule"]["pattern"]
        for good in ["0 3 * * *", "*/15 * * * *", "30 1 1 */2 Mon", "0 0 * * 0"]:
            assert re.search(pattern, good), f"{good!r} must match {pattern!r}"

    def test_schedule_pattern_rejects_garbage(self):
        import re

        pattern = _spec_props("k8sibackups.k8si.io")["schedule"]["pattern"]
        for bad in ["not-a-cron", "", "0 3 * *", "@daily @weekly"]:
            assert not re.search(pattern, bad), f"{bad!r} must NOT match {pattern!r}"


class TestCrdRetentionAndNumericMinimums:
    def test_retention_counts_have_minimum_1(self):
        retention = _spec_props("k8sibackups.k8si.io")["retention"]["properties"]
        for key in ("daily", "weekly", "monthly"):
            assert retention[key].get("minimum") == 1, f"retention.{key} needs minimum: 1"

    def test_job_timeout_has_minimum(self):
        assert _spec_props("k8sibackups.k8si.io")["jobTimeout"].get("minimum") == 60

    def test_max_retries_per_day_has_minimum(self):
        assert _spec_props("k8sibackups.k8si.io")["maxRetriesPerDay"].get("minimum") == 0


class TestCrdStatusMaxItems:
    """recentBackups/recentRuns feed the dashboard sparkline and are writable via
    the status subresource — unbounded arrays can wedge a CR against the ~1.5MB
    etcd object limit (goals-doc #2/#10)."""

    def test_recent_backups_max_items_30(self):
        assert _status_props()["recentBackups"].get("maxItems") == 30

    def test_recent_runs_max_items_30(self):
        assert _status_props()["recentRuns"].get("maxItems") == 30

    def test_recent_runs_entries_have_required_fields(self):
        items = _status_props()["recentRuns"]["items"]
        for field in ("name", "time", "result", "snapshotId", "sizeBytes", "backendType"):
            assert field in items["properties"], f"recentRuns entry missing {field}"

    def test_last_run_log_capped(self):
        assert _status_props()["lastRunLog"].get("maxItems") == 200


class TestCrdImmutability:
    """spec.pvc and the secret refs define WHICH dataset is backed up and WHERE
    the credentials live. Mutating them silently repoints a repo at a different
    volume/dataset, so they are immutable after creation (CEL)."""

    def test_pvc_is_immutable(self):
        validations = _spec_props("k8sibackups.k8si.io")["pvc"].get("x-kubernetes-validations")
        assert validations, "spec.pvc needs an x-kubernetes-validations immutability rule"
        assert any(
            "self" in r.get("rule", "") and "oldSelf" in r.get("rule", "") for r in validations
        )

    def test_secret_refs_are_immutable(self):
        props = _spec_props("k8sibackups.k8si.io")
        for field in ("resticSecret", "kopiaSecret", "repositoryPVC"):
            validations = props[field].get("x-kubernetes-validations")
            assert validations, f"spec.{field} needs an immutability rule"

    def test_immutability_rules_have_messages(self):
        props = _spec_props("k8sibackups.k8si.io")
        for field in ("pvc", "resticSecret", "kopiaSecret", "repositoryPVC"):
            for rule in props[field]["x-kubernetes-validations"]:
                assert rule.get("message"), f"spec.{field} rule needs a human message"

    def test_pvc_remains_required(self):
        spec_required = _openapi(DEPLOY / "crd.yaml", "k8sibackups.k8si.io")["properties"][
            "spec"
        ].get("required", [])
        assert "pvc" in spec_required and "schedule" in spec_required


class TestCrdRunValidation:
    def test_backuprun_status_log_capped(self):
        status = _openapi(DEPLOY / "crd_run.yaml", "k8sibackupruns.k8si.io")["properties"][
            "status"
        ]["properties"]
        assert status["log"]["items"]["properties"], "run log entries must stay structural"
        assert status["log"].get("maxItems") == 200

    def test_backuprun_spec_required_fields(self):
        spec = _openapi(DEPLOY / "crd_run.yaml", "k8sibackupruns.k8si.io")["properties"]["spec"]
        for field in ("backupRef", "triggeredBy", "triggeredAt", "mode"):
            assert field in spec["required"]

    def test_both_crds_are_served_and_namespaced(self):
        for path, name in (
            (DEPLOY / "crd.yaml", "k8sibackups.k8si.io"),
            (DEPLOY / "crd_run.yaml", "k8sibackupruns.k8si.io"),
        ):
            crd = _find(_docs(path), "CustomResourceDefinition", name)
            assert crd["spec"]["scope"] == "Namespaced"
            versions = crd["spec"]["versions"]
            assert any(v["served"] and v["storage"] for v in versions)
            assert crd["spec"]["group"] == "k8si.io"
            assert crd["spec"]["names"]["plural"]


class TestManifestsParse:
    """Every deploy/ manifest must round-trip as valid multi-doc YAML."""

    def test_all_deploy_manifests_parse(self):
        paths = sorted(DEPLOY.glob("*.yaml"))
        assert len(paths) >= 6, f"expected the deploy set, got {[p.name for p in paths]}"
        for path in paths:
            docs = _docs(path)
            assert docs, f"{path.name} parsed to nothing"
            for doc in docs:
                assert "apiVersion" in doc and "kind" in doc, f"{path.name} malformed doc"

    def test_crd_schemas_are_structural(self):
        for path, name in (
            (DEPLOY / "crd.yaml", "k8sibackups.k8si.io"),
            (DEPLOY / "crd_run.yaml", "k8sibackupruns.k8si.io"),
        ):
            _assert_structural(_openapi(path, name), name)

    def test_ci_workflow_parses(self):
        wf = yaml.safe_load(CI.read_text())
        assert wf["jobs"], "ci.yml must keep its jobs"


class TestCiSchedule:
    """The nightly e2e slot must stay clear of the storage node's I/O windows.

    e2e lands on the Open-CAS-cached RAID5 (topolvm-stor01 on hoeve-stor01).
    Two windows are off-limits, both UTC: the k8si backup window 01:00-06:30
    (every K8siBackup CR in the cluster fires there) and the Open-CAS nightly
    cache flush 05:00 UTC + hours of SMR drain (opencas-nightly-flush.timer,
    ~4.1 MB/s). Nightly e2e also must not re-run the buildah jobs.
    """

    def _triggers(self) -> dict:
        return yaml.safe_load(CI.read_text()).get(True) or yaml.safe_load(CI.read_text())["on"]

    def test_schedule_exists(self):
        assert self._triggers().get("schedule"), "nightly e2e schedule is missing"

    def test_schedule_runs_outside_backup_and_flush_windows(self):
        cron = self._triggers()["schedule"][0]["cron"]
        hour = int(cron.split()[1])
        # Allowed: 20-23 UTC (22:00-01:00 local). Forbidden: 01-06:30 UTC (k8si
        # backup window), 05-10 UTC (flush start + SMR drain), 06-20 UTC (family
        # daytime hours). e2e has a 30-minute timeout, so a 23:00 UTC start still
        # clears the 01:00 UTC backup window with an hour to spare.
        assert 20 <= hour <= 23, (
            f"schedule hour {hour}:00 UTC collides with the k8si backup window "
            "(01:00-06:30), the Open-CAS flush drain (05:00-~10:00) or family hours"
        )

    def test_heavy_build_jobs_skip_on_schedule(self):
        wf = yaml.safe_load(CI.read_text())
        for job in (
            "docker-build-arm64",
            "docker-build-amd64",
            "docker-build-ui-arm64",
            "docker-build-ui-amd64",
            "docker-manifest",
            "release",
        ):
            cond = wf["jobs"][job].get("if", "")
            assert "schedule" in cond, f"{job} must skip on schedule events"

    def test_e2e_runs_on_schedule(self):
        cond = yaml.safe_load(CI.read_text())["jobs"]["e2e"]["if"]
        assert "schedule" in cond, "e2e must still run on the nightly schedule"
